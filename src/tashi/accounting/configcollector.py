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

import logging
import threading
import sys
import json
import time

from tashi.rpycservices import rpyctypes as types

from tashi import createClient

"""
Configuration collector for vQuery-style configuration changes
"""

class RPCTypeEncoder(json.JSONEncoder):
	"""Encode RPyC types as deltas for configuration change collection"""
	###XXX: this is something of a hack; probably shouldn't really use __dict__ ?
	def default(self, o):
		return o.__dict__

	def get_uid(self, o):
		"""Get a unique identifier for an RPC type object"""
		if isinstance(o, types.Instance):
			return "instance-%s" % (o.id)
		elif isinstance(o, types.Host):
			return "host-%s" % (o.id)
		#TODO: add other types
		else:
			self.log.warning("Unknown RPC type: %s" % (o.__class__.__name__))
			return "unknown"

	def encode(self, o):
		return json.JSONEncoder.encode(self, o)

	def encode_delta(self, o, changetype, uid=None):
		"""
		JSON-encode an RPC type with identifier in "delta" format
		changetype: one of ADD, CHANGE, or REMOVE
		uid: may be specified to force the uid, else get it from the object
		"""
		if uid == None:
			uid = self.get_uid(o)

		toenc = {
			"time": int(time.time()*1000), #time in ms since the epoch
			"type": changetype,
			"uid": uid,
			"map": o
		}
		return json.JSONEncoder.encode(self, toenc)

class StateMap():
	"""
	Store a state of the system as observed by a ConfigCollector
	"""

	def __init__(self, encoder):
		"""
		encoder: the object -> string encoder to be used when serializing map range elements
		"""
		self.encoder = encoder
		#map of UIDs to (for now) encoded objects
		self.prev = dict()
		#objects touched in last iteration
		self.touched = None

	def start_recv(self):
		"""Start a full update cycle"""
		self.touched = set()

	def recv(self, obj):
		"""
		Update the StateMap with an object.
		Returns a list of all deltas needed to update the StateMap
		"""

		uid = self.encoder.get_uid(obj)
		encoded = self.encoder.encode(obj)
		changetype = None

		if uid in self.prev:
			if encoded != self.prev[uid]:
				changetype = "CHANGE"
			#else there has been no change
		else:
			changetype = "ADD"

		self.touched.add(uid)
		if changetype is None:
			return []
		else:
			self.prev[uid] = encoded
			return [self.encoder.encode_delta(obj,changetype)]

	def recv_list(self, objs):
		"""recv() multiple objects"""
		deltas = []
		for obj in objs:
			deltas.extend(self.recv(obj))
		return deltas

	def end_recv(self):
		"""
		Return deltas required to remove all elements that were removed in this cycle
		"""
		removed = set(self.prev.keys()) - self.touched
		for k in removed:
			del self.prev[k]

		deltas = []
		for k in removed:
			deltas.append(self.encoder.encode_delta(None,"REMOVE",k))

		return deltas

class ConfigCollector(object):
	"""RPC service for the ConfigCollector"""
	CFGNAME = "ConfigCollector"

	def __init__(self, config):
		self.log = logging.getLogger(__name__)
		self.log.setLevel(logging.INFO)

		self.config = config
		self.pollSleep = self.config.getint(ConfigCollector.CFGNAME, "pollSleep")

		self.cm = createClient(config)

		self.smap = StateMap(RPCTypeEncoder())

		threading.Thread(target=self.__start).start()

	def __start(self):

		while True:
			try:
				self.smap.start_recv()
				deltas = []
				
				#update map with all objects in system state
				deltas.extend(self.smap.recv_list(self.cm.getInstances()))
				deltas.extend(self.smap.recv_list(self.cm.getHosts()))
				#TODO: add other types

				deltas.extend(self.smap.end_recv())

				for delta in deltas:
					self.log.info(delta)
			except:
				self.log.warning("ConfigCollector iteration failed: %s" % (sys.exc_info()[0]))

			# wait to do the next iteration
			time.sleep(self.pollSleep)
