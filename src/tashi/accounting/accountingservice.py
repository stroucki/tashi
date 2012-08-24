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

from tashi.util import instantiateImplementation

class AccountingService(object):
	"""RPC service for the Accounting service"""

	def __init__(self, config):
		self.log = logging.getLogger(__name__)
		self.log.setLevel(logging.INFO)

		self.config = config
		self.hooks = []

		self.pollSleep = None

		# XXXstroucki new python has fallback values
		try:			
			# load hooks
			items = self.config.items("AccountingService")
			items.sort()
			for item in items:
					(name, value) = item
					name = name.lower()
					if (name.startswith("hook")):
							try:
									self.hooks.append(instantiateImplementation(value, self.config))
							except:
									self.log.exception("Failed to load hook %s" % (value))
		except:
			pass


	# remote
	def record(self, strings):
		for string in strings:
			self.log.info("Remote: %s" % (string))
