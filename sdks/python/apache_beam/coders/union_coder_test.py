#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# pytype: skip-file

import logging
import unittest

from apache_beam.coders import coders
from apache_beam.coders.union_coder import UnionCoder
from apache_beam.coders.avro_record import AvroRecord
from apache_beam.coders.typecoders import registry


class AvroTestCoder(coders.AvroGenericCoder):
  SCHEMA = """
  {
    "type": "record", "name": "test",
    "fields": [
      {"name": "name", "type": "string"},
      {"name": "age", "type": "int"}
    ]
  }
  """

  def __init__(self):
    super().__init__(self.SCHEMA)


class UnionCoderTest(unittest.TestCase):
  def test_basics(self):
    registry.register_coder(AvroRecord, AvroTestCoder)

    coder = UnionCoder()

    self.assertEqual(coder.is_deterministic(), False)

    assert coder.to_type_hint()
    assert str(coder)

    ar = AvroRecord({"name": "Daenerys targaryen", "age": 23})
    self.assertEqual(coder.decode(coder.encode(ar)).record, ar.record)

    for v in [8, 8.0, bytes(8), True, "8"]:
      self.assertEqual(v, coder.decode(coder.encode(v)))


if __name__ == '__main__':
  logging.getLogger().setLevel(logging.INFO)
  unittest.main()