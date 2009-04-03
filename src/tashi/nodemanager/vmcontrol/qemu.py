# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
# 
#   http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.    

import cPickle
import logging
import os
import threading
import random
import select
import signal
import socket
import subprocess
import sys
import time

from tashi.services.ttypes import *
from tashi.util import broken, logged, scrubString
from vmcontrolinterface import VmControlInterface

log = logging.getLogger(__file__)

def controlConsole(child, port):
	"""This exposes a TCP port that connects to a particular child's monitor -- used for debugging"""
	#print "controlConsole"
	listenSocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
	listenSocket.bind(("0.0.0.0", port))
	#print "bound"
	try:
		try:
			listenSocket.listen(5)
			ls = listenSocket.fileno()
			input = child.monitorFd
			output = child.monitorFd
			#print "listen"
			select.select([ls], [], [])
			(s, clientAddr) = listenSocket.accept()
			while s:
				if (output != -1):
					(rl, wl, el) = select.select([s, output], [], [])
				else:
					(rl, wl, el) = select.select([s], [], [])
				if (len(rl) > 0):
					if (rl[0] == s):
						#print "from s"
						buf = s.recv(4096)
						if (buf == ""):
							s.close()
							listenSocket.close()
							s = None
							continue
						if (output != -1):
							os.write(child.monitorFd, buf)
					elif (rl[0] == output):
						#print "from output"
						buf = os.read(output, 4096)
						#print "read complete"
						if (buf == ""):
							output = -1
						else:
							s.send(buf)
		except:
			s.close()
			listenSocket.close()
	finally:
		#print "Thread exiting"
		pass

class Qemu(VmControlInterface):
	"""This class implements the VmControlInterface for Qemu/KVM"""
	
	def __init__(self, config, dfs, nm):
		VmControlInterface.__init__(self, config, dfs, nm)
		self.QEMU_BIN = self.config.get("Qemu", "qemuBin")
		self.INFO_DIR = self.config.get("Qemu", "infoDir")
		self.POLL_DELAY = float(self.config.get("Qemu", "pollDelay"))
		self.migrationRetries = int(self.config.get("Qemu", "migrationRetries"))
		self.monitorTimeout = float(self.config.get("Qemu", "monitorTimeout"))
		self.migrateTimeout = float(self.config.get("Qemu", "migrateTimeout"))
		self.controlledVMs = {}
		self.usedPorts = []
		self.usedPortsLock = threading.Lock()
		self.vncPorts = []
		self.vncPortLock = threading.Lock()
		self.consolePort = 10000
		self.consolePortLock = threading.Lock()
		self.migrationSemaphore = threading.Semaphore(int(self.config.get("Qemu", "maxParallelMigrations")))
		try:
			os.mkdir(self.INFO_DIR)
		except:
			pass
		self.scanInfoDir()
		threading.Thread(target=self.pollVMsLoop).start()
	
	class anonClass:
		def __init__(self, **attrs):
			self.__dict__.update(attrs)
	
	def getSystemPids(self):
		"""Utility function to get a list of system PIDs that match the QEMU_BIN specified (/proc/nnn/exe)"""
		pids = []
		for f in os.listdir("/proc"):
			try:
				bin = os.readlink("/proc/%s/exe" % (f))
				if (bin.find(self.QEMU_BIN) != -1):
					pids.append(int(f))
			except Exception:
				pass
		return pids
	
	def matchSystemPids(self, controlledVMs):
		"""This is run in a separate polling thread and it must do things that are thread safe"""
		vmIds = controlledVMs.keys()
		pids = self.getSystemPids()
		for vmId in vmIds:
			if vmId not in pids:
				os.unlink(self.INFO_DIR + "/%d"%(vmId))
				child = controlledVMs[vmId]
				del controlledVMs[vmId]
				if (child.vncPort >= 0):
					self.vncPortLock.acquire()
					self.vncPorts.remove(child.vncPort)
					self.vncPortLock.release()
				log.info("Removing vmId %d" % (vmId))
				if (child.OSchild):
					os.waitpid(vmId, 0)
				if (child.errorBit):
					if (child.OSchild):
						f = open("/tmp/%d.err" % (vmId), "w")
						f.write(child.stderr.read())
						f.close()
					f = open("/tmp/%d.pty" % (vmId), "w")
					for i in child.monitorHistory:
						f.write(i)
					f.close()
				try:
					if (not child.migratingOut):
						self.nm.vmStateChange(vmId, None, InstanceState.Exited)
				except Exception, e:
					log.exception("vmStateChange failed")
						
	
	def scanInfoDir(self):
		"""This is not thread-safe and must only be used during class initialization"""
		controlledVMs = {}
		controlledVMs.update(map(lambda x: (int(x), self.anonClass(OSchild=False, errorBit=False, migratingOut=False)), os.listdir(self.INFO_DIR + "/")))
		if (len(controlledVMs) == 0):
			log.info("No vm information found in %s", self.INFO_DIR)
		for vmId in controlledVMs:
			try:
				child = self.loadChildInfo(vmId)
				self.vncPortLock.acquire()
				if (child.vncPort >= 0):
					self.vncPorts.append(child.vncPort)
				self.vncPortLock.release()
				child.monitorFd = os.open(child.ptyFile, os.O_RDWR | os.O_NOCTTY)
				child.monitor = os.fdopen(child.monitorFd)
				self.controlledVMs[child.pid] = child
				log.info("Adding vmId %d" % (child.pid))
			except Exception, e:
				log.exception("Failed to load VM info for %d", vmId)
			else:
				log.info("Loaded VM info for %d", vmId)
		self.matchSystemPids(self.controlledVMs)
	
	def pollVMsLoop(self):
		"""Infinite loop that checks for dead VMs"""
		while True:
			self.matchSystemPids(self.controlledVMs)
			time.sleep(self.POLL_DELAY)
	
	def waitForExit(self, vmId):
		"""This waits until an element is removed from the dictionary -- the polling thread must detect an exit"""
		while vmId in self.controlledVMs:
			time.sleep(self.POLL_DELAY)
	
	def getChildFromPid(self, pid):
		"""Do a simple dictionary lookup, but raise a unique exception if the key doesn't exist"""
		child = self.controlledVMs.get(pid, None)
		if (not child):
			raise Exception, "Uncontrolled vmId %d" % (pid)
		return child
	
	def getControlConsole(self, vmId, port):
		"""Spawn a thread that attaches the control console of a particular Qemu to a TCP port -- used for debugging only"""
		child = self.getChildFromPid(vmId)
		threading.Thread(target=(lambda: controlConsole(child, port))).start()
		return port
	
	def consumeAvailable(self, child):
		"""Consume characters one-by-one until they stop coming"""
		monitorFd = child.monitorFd
		buf = ""
		try:
			(rlist, wlist, xlist) = select.select([monitorFd], [], [], 0.0)
			while (len(rlist) > 0):
				c = os.read(monitorFd, 1)
				if (c == ""):
					log.error("Early termination on monitor for vmId %d" % (child.pid))
					child.errorBit = True
					raise RuntimeError
				buf = buf + c
				(rlist, wlist, xlist) = select.select([monitorFd], [], [], 0.0)
		finally:
			child.monitorHistory.append(buf)
		return buf
	
	def consumeUntil(self, child, needle, timeout = -1):
		"""Consume characters one-by-one until something specific comes up"""
		if (timeout == -1):
			timeout = self.monitorTimeout
		monitorFd = child.monitorFd
		buf = " " * len(needle)
		try:
			while (buf[-(len(needle)):] != needle):
				#print "[BUF]: %s" % (buf)
				#print "[NEE]: %s" % (needle)
				(rlist, wlist, xlist) = select.select([monitorFd], [], [], timeout)
				if (len(rlist) == 0):
					log.error("Timeout getting results from monitor for vmId %d" % (child.pid))
					child.errorBit = True
					raise RuntimeError
				c = os.read(monitorFd, 1)
				if (c == ""):
					log.error("Early termination on monitor for vmId %d" % (child.pid))
					child.errorBit = True
					raise RuntimeError
				buf = buf + c
		finally:
			child.monitorHistory.append(buf[len(needle):])
		return buf[len(needle):]
		
	def enterCommand(self, child, command, expectPrompt = True, timeout = -1):
		"""Enter a command on the qemu monitor"""
		res = self.consumeAvailable(child)
		os.write(child.monitorFd, command + "\n")
		if (expectPrompt):
			self.consumeUntil(child, command)
			res = self.consumeUntil(child, "(qemu) ", timeout=timeout)
		return res

	def genTmpFilename(self):
		"""Create a temporary file name for a fifo used to uncompress a suspended VM"""
		return "/tmp/Qemu_%d" % (os.getpid())
	
	def loadChildInfo(self, vmId):
		child = self.anonClass(pid=vmId)
		info = open(self.INFO_DIR + "/%d"%(child.pid), "r")
		(instance, pid, ptyFile) = cPickle.load(info)
		info.close()
		if (pid != child.pid):
			raise Exception, "PID mismatch"
		child.instance = instance
		child.pid = pid
		child.ptyFile = ptyFile
		if ('monitorHistory' not in child.__dict__):
			child.monitorHistory = []
		if ('OSchild' not in child.__dict__):
			child.OSchild = False
		if ('errorBit' not in child.__dict__):
			child.errorBit = False
		if ('migratingOut' not in child.__dict__):
			child.migratingOut = False
		if ('vncPort' not in child.__dict__):
			child.vncPort = -1
		return child
	
	def saveChildInfo(self, child):
		info = open(self.INFO_DIR + "/%d"%(child.pid), "w")
		cPickle.dump((child.instance, child.pid, child.ptyFile), info)
		info.close()

	def startVm(self, instance, source):
		"""Universal function to start a VM -- used by instantiateVM, resumeVM, and prepReceiveVM"""
		clockString = instance.hints.get("clock", "dynticks")
		diskInterface = instance.hints.get("diskInterface", "ide")
		diskString = ""
		for index in range(0, len(instance.disks)):
			disk = instance.disks[index]
			uri = scrubString(disk.uri)
			imageLocal = self.dfs.getLocalHandle("images/" + uri)
			if (disk.persistent):
				snapshot = "off"
			else:
				snapshot = "on"
			diskString = diskString + "-drive file=%s,if=%s,index=%d,snapshot=%s,media=disk " % (imageLocal, diskInterface, index, snapshot)
		nicModel = instance.hints.get("nicModel", "e1000")
		nicString = ""
		for nic in instance.nics:
			nicString = nicString + "-net nic,macaddr=%s,model=%s,vlan=%d -net tap,vlan=%d,script=/etc/qemu-ifup.%d " % (nic.mac, nicModel, nic.network, nic.network, nic.network)
		if (not source):
			sourceString = ""
		else:
			sourceString = "-incoming %s" % (source)
		cmd = "%s -clock %s %s %s -m %d -smp %d -serial none -vnc none -monitor pty %s" % (self.QEMU_BIN, clockString, diskString, nicString, instance.memory, instance.cores, sourceString)
		log.info("QEMU command: %s" % (cmd))
		cmd = cmd.split()
		(pipe_r, pipe_w) = os.pipe()
		pid = os.fork()
		if (pid == 0):
			pid = os.getpid()
			os.setpgid(pid, pid)
			os.close(pipe_r)
			os.dup2(pipe_w, sys.stderr.fileno())
			for i in [sys.stdin.fileno(), sys.stdout.fileno()]:
				try:
					os.close(i)
				except:
					pass
			for i in xrange(3, os.sysconf("SC_OPEN_MAX")):
				try:
					os.close(i)
				except:
					pass
			os.execl(self.QEMU_BIN, *cmd)
			sys.exit(-1)
		os.close(pipe_w)
		child = self.anonClass(pid=pid, instance=instance, stderr=os.fdopen(pipe_r, 'r'), migratingOut = False, monitorHistory=[], errorBit = False, OSchild = True)
		child.ptyFile = None
		child.vncPort = -1
		self.saveChildInfo(child)
		self.controlledVMs[child.pid] = child
		log.info("Adding vmId %d" % (child.pid))
		return (child.pid, cmd)

	def getPtyInfo(self, child, issueContinue):
		ptyFile = None
		while not ptyFile:
			l = child.stderr.readline()
			if (l == ""):
				os.waitpid(child.pid, 0)
				raise Exception, "Failed to start VM -- ptyFile not found"
			if (l.find("char device redirected to ") != -1):
				ptyFile=l[26:].strip()
				break
		child.ptyFile = ptyFile
		child.monitorFd = os.open(child.ptyFile, os.O_RDWR | os.O_NOCTTY)
		child.monitor = os.fdopen(child.monitorFd)
		self.saveChildInfo(child)
		if (issueContinue):
			self.enterCommand(child, "c")
	
	def stopVm(self, vmId, target, stopFirst):
		"""Universal function to stop a VM -- used by suspendVM, migrateVM """
		child = self.getChildFromPid(vmId)
		if (stopFirst):
			self.enterCommand(child, "stop")
		if (target):
			retry = self.migrationRetries
			while (retry > 0):
				res = self.enterCommand(child, "migrate %s" % (target), timeout=self.migrateTimeout)
				retry = retry - 1
				if (res.find("migration failed") == -1):
					retry = -1
				else:
					log.error("Migration (transiently) failed: %s\n", res)
			if (retry == 0):
				log.error("Migration failed: %s\n", res)
				child.errorBit = True
				raise RuntimeError
		self.enterCommand(child, "quit", expectPrompt=False)
		return vmId
	
	def instantiateVm(self, instance):
		(vmId, cmd) = self.startVm(instance, None)
		child = self.getChildFromPid(vmId)
		self.getPtyInfo(child, False)
		child.cmd = cmd
		self.saveChildInfo(child)
		return vmId
	
	def suspendVm(self, vmId, target, suspendCookie):
		child = self.getChildFromPid(vmId)
		info = self.dfs.open("%s.info" % (target), "w")
		cPickle.dump((child.instance, suspendCookie), info)
		info.close()
		# XXX: Use fifo to improve performance
		vmId = self.stopVm(vmId, "\"exec:gzip -c > /tmp/%s.dat\"" % (target), True)
		self.dfs.copyTo("/tmp/%s.dat" % (target), "%s.dat" % (target))
		return vmId
	
	def resumeVm(self, source):
		# XXX: Read in and unzip directly (or use fifo)
		self.dfs.copyFrom("%s.dat" % (source), "/tmp/%s.dat" % (source))
		info = self.dfs.open("%s.info" % (source), "r")
		(instance, suspendCookie) = cPickle.load(info)
		info.close()
		tmpFile = self.genTmpFilename()
		os.mkfifo(tmpFile)
		zcat = subprocess.Popen(args=["/bin/bash", "-c", "zcat /tmp/%s.dat > %s" % (source, tmpFile)], executable="/bin/bash", close_fds=True)
		(vmId, cmd) = self.startVm(instance, "file://%s" % tmpFile)
		zcat.wait()
		os.unlink(tmpFile)
		child = self.getChildFromPid(vmId)
		self.getPtyInfo(child, True)
		child.suspendCookie = suspendCookie
		child.cmd = cmd
		return (vmId, suspendCookie)
	
	def prepReceiveVm(self, instance, source):
		self.usedPortsLock.acquire()
		port = int(random.random()*1000+19000)
		while port in self.usedPorts:
			port = int(random.random()*1000+19000)
		self.usedPorts.append(port)
		self.usedPortsLock.release()
		(vmId, cmd) = self.startVm(instance, "tcp:0.0.0.0:%d" % (port))
		transportCookie = cPickle.dumps((port, vmId, socket.gethostname()))
		child = self.getChildFromPid(vmId)
		child.cmd = cmd
		child.transportCookie = transportCookie
		self.saveChildInfo(child)
		# XXX: Cleanly wait until the port is open
		lc = 0
		while (lc < 1):
			(stdin, stdout) = os.popen2("netstat -ln | grep 0.0.0.0:%d | wc -l" % (port))
			stdin.close()
			r = stdout.read()
			lc = int(r.strip())
			if (lc < 1):
				time.sleep(1.0)
		return transportCookie
	
	def migrateVm(self, vmId, target, transportCookie):
		self.migrationSemaphore.acquire()
		try:
			(port, _vmId, _hostname) = cPickle.loads(transportCookie)
			child = self.getChildFromPid(vmId)
			child.migratingOut = True
			res = self.stopVm(vmId, "tcp:%s:%d" % (target, port), False)
			# XXX: Some sort of feedback would be nice
			# XXX: Should we block?
			self.waitForExit(vmId)
		finally:
			self.migrationSemaphore.release()
		return res
	
	def receiveVm(self, transportCookie):
		(port, vmId, _hostname) = cPickle.loads(transportCookie)
		try:
			child = self.getChildFromPid(vmId)
		except:
			log.error("Failed to get child info; transportCookie = %s; hostname = %s" % (str(cPickle.loads(transportCookie)), socket.hostname()))
			raise
		try:
			self.getPtyInfo(child, True)
		except RuntimeError, e:
			log.error("Failed to get pty info -- VM likely died")
			child.errorBit = True
			raise
		self.usedPortsLock.acquire()
		self.usedPorts = filter(lambda _port: _port != port, self.usedPorts)
		self.usedPortsLock.release()
		return vmId
	
	def pauseVm(self, vmId):
		child = self.getChildFromPid(vmId)
		self.enterCommand(child, "stop")
	
	def unpauseVm(self, vmId):
		child = self.getChildFromPid(vmId)
		self.enterCommand(child, "c")
	
	@broken
	def shutdownVm(self, vmId):
		"""'system_powerdown' doesn't seem to actually shutdown the VM"""
		child = self.getChildFromPid(vmId)
		self.enterCommand(child, "system_powerdown")
	
	def destroyVm(self, vmId):
		child = self.getChildFromPid(vmId)
		child.migratingOut = False
		# XXX: the child could have exited between these two points, but I don't know how to fix that since it might not be our child process
		os.kill(child.pid, signal.SIGKILL)
	
	def vmmSpecificCall(self, vmId, arg):
		arg = arg.lower()
		if (arg == "startvnc"):
			child = self.getChildFromPid(vmId)
			hostname = socket.gethostname()
			if (child.vncPort == -1):
				self.vncPortLock.acquire()
				port = 0
				while (port in self.vncPorts):
					port = port + 1
				self.vncPorts.append(port)
				self.vncPortLock.release()
				self.enterCommand(child, "change vnc :%d" % (port))
				child.vncPort = port
				self.saveChildInfo(child)
			port = child.vncPort
			return "VNC started on %s:%d" % (hostname, port+5900)
		elif (arg == "stopvnc"):
			child = self.getChildFromPid(vmId)
			self.enterCommand(child, "change vnc none")
			if (child.vncPort != -1):
				self.vncPortLock.acquire()
				self.vncPorts.remove(child.vncPort)
				self.vncPortLock.release()
				child.vncPort = -1
				self.saveChildInfo(child)
			return "VNC halted"
		elif (arg.startswith("changecdrom:")):
			child = self.getChildFromPid(vmId)
			iso = scrubString(arg[12:])
			imageLocal = self.dfs.getLocalHandle("images/" + iso)
			self.enterCommand(child, "change ide1-cd0 %s" % (imageLocal))
			return "Changed ide1-cd0 to %s" % (iso)
		elif (arg == "startconsole"):
			child = self.getChildFromPid(vmId)
			hostname = socket.gethostname()
			self.consolePortLock.acquire()
			consolePort = self.consolePort
			self.consolePort = self.consolePort+1
			self.consolePortLock.release()
			threading.Thread(target=lambda: controlConsole(child,consolePort)).start()
			return "Control console listenting on %s:%d" % (hostname, consolePort)
		else:
			return "Unknown arg %s" % (arg)
	
	def listVms(self):
		return self.controlledVMs.keys()
