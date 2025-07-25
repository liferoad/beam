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

"""
Functionality to perform file loads into BigQuery for Batch and Streaming
pipelines.

This source is able to work around BigQuery load quotas and limitations. When
destinations are dynamic, or when data for a single job is too large, the data
will be split into multiple jobs.

NOTHING IN THIS FILE HAS BACKWARDS COMPATIBILITY GUARANTEES.
"""

# pytype: skip-file

import hashlib
import io
import logging
import random
import time
import uuid

import apache_beam as beam
from apache_beam import pvalue
from apache_beam.io import filesystems as fs
from apache_beam.io.gcp import bigquery_tools
from apache_beam.io.gcp.bigquery_io_metadata import create_bigquery_io_metadata
from apache_beam.metrics.metric import Lineage
from apache_beam.options import value_provider as vp
from apache_beam.options.pipeline_options import GoogleCloudOptions
from apache_beam.transforms import trigger
from apache_beam.transforms import util
from apache_beam.transforms.display import DisplayDataItem
from apache_beam.transforms.window import GlobalWindows

# Protect against environments where bigquery library is not available.
# pylint: disable=wrong-import-order, wrong-import-position
try:
  from apitools.base.py.exceptions import HttpError
except ImportError:
  pass

_LOGGER = logging.getLogger(__name__)

ONE_TERABYTE = (1 << 40)

# The maximum file size for imports is 5TB. We keep our files under that.
_DEFAULT_MAX_FILE_SIZE = 4 * ONE_TERABYTE

_DEFAULT_MAX_WRITERS_PER_BUNDLE = 20

# The maximum size for a single load job is one terabyte
_MAXIMUM_LOAD_SIZE = 15 * ONE_TERABYTE

# Big query only supports up to 10 thousand URIs for a single load job.
_MAXIMUM_SOURCE_URIS = 10 * 1000

# If triggering_frequency is supplied, we will trigger the file write after
# this many records are written.
_FILE_TRIGGERING_RECORD_COUNT = 500000

# If using auto-sharding for unbounded data, we batch the records before
# triggering file write to avoid generating too many small files.
_FILE_TRIGGERING_BATCHING_DURATION_SECS = 1

# How many seconds we wait before polling a pending job
_SLEEP_DURATION_BETWEEN_POLLS = 10


def _generate_job_name(job_name, job_type, step_name):
  return bigquery_tools.generate_bq_job_name(
      job_name=job_name,
      step_id=step_name,
      job_type=job_type,
      random=_bq_uuid())


def file_prefix_generator(
    with_validation=True, pipeline_gcs_location=None, temp_location=None):
  def _generate_file_prefix(unused_elm):
    # If a gcs location is provided to the pipeline, then we shall use that.
    # Otherwise, we shall use the temp_location from pipeline options.
    gcs_base = pipeline_gcs_location.get()
    if not gcs_base:
      gcs_base = temp_location

    # This will fail at pipeline execution time, but will fail early, as this
    # step doesn't have any dependencies (and thus will be one of the first
    # stages to be run).
    if with_validation and (not gcs_base or not gcs_base.startswith('gs://')):
      raise ValueError(
          'Invalid GCS location: %r.\n'
          'Writing to BigQuery with FILE_LOADS method requires a'
          ' GCS location to be provided to write files to be loaded'
          ' into BigQuery. Please provide a GCS bucket through'
          ' custom_gcs_temp_location in the constructor of WriteToBigQuery'
          ' or the fallback option --temp_location, or pass'
          ' method="STREAMING_INSERTS" to WriteToBigQuery.' % gcs_base)

    prefix_uuid = _bq_uuid()
    return fs.FileSystems.join(gcs_base, 'bq_load', prefix_uuid)

  return _generate_file_prefix


def _make_new_file_writer(
    file_prefix,
    destination,
    file_format,
    schema=None,
    schema_side_inputs=tuple()):
  destination = bigquery_tools.get_hashable_destination(destination)

  # Windows does not allow : on filenames. Replacing with underscore.
  # Other disallowed characters are:
  # https://docs.microsoft.com/en-us/windows/desktop/fileio/naming-a-file
  destination = destination.replace(':', '.')

  directory = fs.FileSystems.join(file_prefix, destination)

  if not fs.FileSystems.exists(directory):
    fs.FileSystems.mkdirs(directory)

  file_name = str(uuid.uuid4())
  file_path = fs.FileSystems.join(file_prefix, destination, file_name)

  if file_format == bigquery_tools.FileFormat.AVRO:
    if callable(schema):
      schema = schema(destination, *schema_side_inputs)
    elif isinstance(schema, vp.ValueProvider):
      schema = schema.get()

    writer = bigquery_tools.AvroRowWriter(
        fs.FileSystems.create(file_path, "application/avro"), schema)
  elif file_format == bigquery_tools.FileFormat.JSON:
    writer = bigquery_tools.JsonRowWriter(
        fs.FileSystems.create(file_path, "application/text"))
  else:
    raise ValueError((
        'Only AVRO and JSON are supported as intermediate formats for '
        'BigQuery WriteRecordsToFile, got: {}.').format(file_format))

  return file_path, writer


def _bq_uuid(seed=None):
  if not seed:
    return str(uuid.uuid4()).replace("-", "")
  else:
    return str(hashlib.md5(seed.encode('utf8')).hexdigest())


class _ShardDestinations(beam.DoFn):
  """Adds a shard number to the key of the KV element.

  Experimental; no backwards compatibility guarantees."""
  DEFAULT_SHARDING_FACTOR = 10

  def __init__(self, sharding_factor=DEFAULT_SHARDING_FACTOR):
    self.sharding_factor = sharding_factor

  def start_bundle(self):
    self._shard_count = random.randrange(self.sharding_factor)

  def process(self, element):
    destination = element[0]
    row = element[1]

    sharded_destination = (
        destination, self._shard_count % self.sharding_factor)
    self._shard_count += 1
    yield (sharded_destination, row)


class WriteRecordsToFile(beam.DoFn):
  """Write input records to files before triggering a load job.

  This transform keeps up to ``max_files_per_bundle`` files open to write to. It
  receives (destination, record) tuples, and it writes the records to different
  files for each destination.

  If there are more than ``max_files_per_bundle`` destinations that we need to
  write to, then those records are grouped by their destination, and later
  written to files by ``WriteGroupedRecordsToFile``.

  It outputs two PCollections.
  """

  UNWRITTEN_RECORD_TAG = 'UnwrittenRecords'
  WRITTEN_FILE_TAG = 'WrittenFiles'

  def __init__(
      self,
      schema,
      max_files_per_bundle=_DEFAULT_MAX_WRITERS_PER_BUNDLE,
      max_file_size=_DEFAULT_MAX_FILE_SIZE,
      file_format=None):
    """Initialize a :class:`WriteRecordsToFile`.

    Args:
      max_files_per_bundle (int): The maximum number of files that can be kept
        open during execution of this step in a worker. This is to avoid over-
        whelming the worker memory.
      max_file_size (int): The maximum size in bytes for a file to be used in
        an export job.

    """
    self.schema = schema
    self.max_files_per_bundle = max_files_per_bundle
    self.max_file_size = max_file_size
    self.file_format = file_format or bigquery_tools.FileFormat.JSON

  def display_data(self):
    return {
        'max_files_per_bundle': self.max_files_per_bundle,
        'max_file_size': str(self.max_file_size),
        'file_format': self.file_format,
    }

  def start_bundle(self):
    self._destination_to_file_writer = {}

  def process(self, element, file_prefix, *schema_side_inputs):
    """Take a tuple with (destination, row) and write to file or spill out.

    Destination may be a ``TableReference`` or a string, and row is a
    Python dictionary for a row to be inserted to BigQuery."""
    destination = bigquery_tools.get_hashable_destination(element[0])
    row = element[1]

    if destination not in self._destination_to_file_writer:
      if len(self._destination_to_file_writer) < self.max_files_per_bundle:
        self._destination_to_file_writer[destination] = _make_new_file_writer(
            file_prefix,
            destination,
            self.file_format,
            self.schema,
            schema_side_inputs)
      else:
        yield pvalue.TaggedOutput(
            WriteRecordsToFile.UNWRITTEN_RECORD_TAG, element)
        return

    (file_path, writer) = self._destination_to_file_writer[destination]

    # TODO(pabloem): Is it possible for this to throw exception?
    writer.write(row)

    file_size = writer.tell()
    if file_size > self.max_file_size:
      writer.close()
      self._destination_to_file_writer.pop(destination)
      yield pvalue.TaggedOutput(
          WriteRecordsToFile.WRITTEN_FILE_TAG,
          (destination, (file_path, file_size)))

  def finish_bundle(self):
    for destination, file_path_writer in \
        self._destination_to_file_writer.items():
      (file_path, writer) = file_path_writer
      file_size = writer.tell()
      writer.close()
      yield pvalue.TaggedOutput(
          WriteRecordsToFile.WRITTEN_FILE_TAG,
          GlobalWindows.windowed_value((destination, (file_path, file_size))))
    self._destination_to_file_writer = {}


class WriteGroupedRecordsToFile(beam.DoFn):
  """Receives collection of dest-iterable(records), writes it to files.

  This is different from ``WriteRecordsToFile`` because it receives records
  grouped by destination. This means that it's not necessary to keep multiple
  file descriptors open, because we know for sure when records for a single
  destination have been written out.

  Experimental; no backwards compatibility guarantees.
  """
  def __init__(
      self, schema, max_file_size=_DEFAULT_MAX_FILE_SIZE, file_format=None):
    self.schema = schema
    self.max_file_size = max_file_size
    self.file_format = file_format or bigquery_tools.FileFormat.JSON

  def process(self, element, file_prefix, *schema_side_inputs):
    destination = bigquery_tools.get_hashable_destination(element[0])
    rows = element[1]

    file_path, writer = None, None

    for row in rows:
      if writer is None:
        (file_path, writer) = _make_new_file_writer(
            file_prefix,
            destination,
            self.file_format,
            self.schema,
            schema_side_inputs)

      writer.write(row)

      file_size = writer.tell()
      if file_size > self.max_file_size:
        writer.close()
        yield (destination, (file_path, file_size))
        file_path, writer = None, None
    if writer is not None:
      writer.close()
      yield (destination, (file_path, file_size))


class UpdateDestinationSchema(beam.DoFn):
  """Update destination schema based on data that is about to be copied into it.

  Unlike load and query jobs, BigQuery copy jobs do not support schema field
  addition or relaxation on the destination table. This DoFn fills that gap by
  updating the destination table schemas to be compatible with the data coming
  from the source table so that schema field modification options are respected
  regardless of whether data is loaded directly to the destination table or
  loaded into temporary tables before being copied into the destination.

  This transform takes as input a (destination, job_reference) pair where the
  job_reference refers to a completed load job into a temporary table.

  This transform emits (destination, job_reference) pairs where the
  job_reference refers to a submitted load job for performing the schema
  modification in JSON format. Note that the input and output job references
  are not the same.

  Experimental; no backwards compatibility guarantees.
  """
  def __init__(
      self,
      project=None,
      write_disposition=None,
      test_client=None,
      additional_bq_parameters=None,
      step_name=None,
      load_job_project_id=None):
    self.project = project
    self._test_client = test_client
    self._write_disposition = write_disposition
    self._additional_bq_parameters = additional_bq_parameters or {}
    self._step_name = step_name
    self._load_job_project_id = load_job_project_id

  def start_bundle(self):
    self.bq_wrapper = bigquery_tools.BigQueryWrapper(client=self._test_client)
    self._bq_io_metadata = create_bigquery_io_metadata(self._step_name)
    self.pending_jobs = []

  def display_data(self):
    return {
        'write_disposition': str(self._write_disposition),
        'additional_bq_params': str(self._additional_bq_parameters),
    }

  def process(self, element, schema_mod_job_name_prefix):
    destination = element[0]
    temp_table_load_job_reference = element[1]

    if callable(self._additional_bq_parameters):
      additional_parameters = self._additional_bq_parameters(destination)
    elif isinstance(self._additional_bq_parameters, vp.ValueProvider):
      additional_parameters = self._additional_bq_parameters.get()
    else:
      additional_parameters = self._additional_bq_parameters

    # When writing to normal tables WRITE_TRUNCATE will overwrite the schema but
    # when writing to a partition, care needs to be taken to update the schema
    # even on WRITE_TRUNCATE.
    if (self._write_disposition not in ('WRITE_TRUNCATE', 'WRITE_APPEND') or
        not additional_parameters or
        not additional_parameters.get("schemaUpdateOptions")):
      # No need to modify schema of destination table
      return

    table_reference = bigquery_tools.parse_table_reference(destination)
    if table_reference.projectId is None:
      table_reference.projectId = vp.RuntimeValueProvider.get_value(
          'project', str, '') or self.project

    try:
      # Check if destination table exists
      destination_table = self.bq_wrapper.get_table(
          project_id=table_reference.projectId,
          dataset_id=table_reference.datasetId,
          table_id=table_reference.tableId)
    except HttpError as exn:
      if exn.status_code == 404:
        # Destination table does not exist, so no need to modify its schema
        # ahead of the copy jobs.
        return
      else:
        raise

    temp_table_load_job = self.bq_wrapper.get_job(
        project=temp_table_load_job_reference.projectId,
        job_id=temp_table_load_job_reference.jobId,
        location=temp_table_load_job_reference.location)
    temp_table_schema = temp_table_load_job.configuration.load.schema

    if bigquery_tools.check_schema_equal(temp_table_schema,
                                         destination_table.schema,
                                         ignore_descriptions=True,
                                         ignore_field_order=True):
      # Destination table schema is already the same as the temp table schema,
      # so no need to run a job to update the destination table schema.
      return

    destination_hash = _bq_uuid(
        '%s:%s.%s' % (
            table_reference.projectId,
            table_reference.datasetId,
            table_reference.tableId))
    uid = _bq_uuid()
    job_name = '%s_%s_%s' % (schema_mod_job_name_prefix, destination_hash, uid)

    _LOGGER.info(
        'Triggering schema modification job %s on %s',
        job_name,
        table_reference)
    # Trigger potential schema modification by loading zero rows into the
    # destination table with the temporary table schema.
    schema_update_job_reference = self.bq_wrapper.perform_load_job(
        destination=table_reference,
        source_stream=io.BytesIO(),  # file with zero rows
        job_id=job_name,
        schema=temp_table_schema,
        write_disposition='WRITE_APPEND',
        create_disposition='CREATE_NEVER',
        additional_load_parameters=additional_parameters,
        job_labels=self._bq_io_metadata.add_additional_bq_job_labels(),
        # JSON format is hardcoded because zero rows load(unlike AVRO) and
        # a nested schema(unlike CSV, which a default one) is permitted.
        source_format="NEWLINE_DELIMITED_JSON",
        load_job_project_id=self._load_job_project_id)
    self.pending_jobs.append(
        GlobalWindows.windowed_value(
            (destination, schema_update_job_reference)))

  def finish_bundle(self):
    # Unlike the other steps, schema update is not always necessary.
    # In that case, return a None value to avoid blocking in streaming context.
    # Otherwise, the streaming pipeline would get stuck waiting for the
    # TriggerCopyJobs side-input.
    if not self.pending_jobs:
      return [GlobalWindows.windowed_value(None)]

    for windowed_value in self.pending_jobs:
      job_ref = windowed_value.value[1]
      self.bq_wrapper.wait_for_bq_job(
          job_ref, sleep_duration_sec=_SLEEP_DURATION_BETWEEN_POLLS)
    return self.pending_jobs


class TriggerCopyJobs(beam.DoFn):
  """Launches jobs to copy from temporary tables into the main target table.

  When a job needs to write to multiple destination tables, or when a single
  destination table needs to have multiple load jobs to write to it, files are
  loaded into temporary tables, and those tables are later copied to the
  destination tables.

  This transform emits (destination, job_reference) pairs.

  TODO(BEAM-7822): In file loads method of writing to BigQuery,
    copying from temp_tables to destination_table is not atomic.
    See: https://issues.apache.org/jira/browse/BEAM-7822
  """

  TRIGGER_DELETE_TEMP_TABLES = 'TriggerDeleteTempTables'

  def __init__(
      self,
      project=None,
      create_disposition=None,
      write_disposition=None,
      test_client=None,
      step_name=None,
      load_job_project_id=None):
    self.project = project
    self.create_disposition = create_disposition
    self.write_disposition = write_disposition
    self.test_client = test_client
    self._observed_tables = set()
    self.bq_io_metadata = None
    self._step_name = step_name
    self.load_job_project_id = load_job_project_id

  def display_data(self):
    return {
        'launchesBigQueryJobs': DisplayDataItem(
            True, label="This Dataflow job launches bigquery jobs.")
    }

  def setup(self):
    self._observed_tables = set()

  def start_bundle(self):
    self.bq_wrapper = bigquery_tools.BigQueryWrapper(client=self.test_client)
    if not self.bq_io_metadata:
      self.bq_io_metadata = create_bigquery_io_metadata(self._step_name)
    self.pending_jobs = []

  def process(
      self, element_list, job_name_prefix=None, unused_schema_mod_jobs=None):
    if isinstance(element_list, tuple):
      # Allow this for streaming update compatibility while fixing BEAM-24535.
      self.process_one(element_list, job_name_prefix)
    else:
      for element in element_list:
        self.process_one(element, job_name_prefix)

  def process_one(self, element, job_name_prefix):
    destination, job_reference = element

    copy_to_reference = bigquery_tools.parse_table_reference(destination)
    if copy_to_reference.projectId is None:
      copy_to_reference.projectId = vp.RuntimeValueProvider.get_value(
          'project', str, '') or self.project

    copy_from_reference = bigquery_tools.parse_table_reference(destination)
    copy_from_reference.tableId = job_reference.jobId
    if copy_from_reference.projectId is None:
      copy_from_reference.projectId = vp.RuntimeValueProvider.get_value(
          'project', str, '') or self.project

    _LOGGER.info(
        "Triggering copy job from %s to %s",
        copy_from_reference,
        copy_to_reference)

    wait_for_job, write_disposition = (
      self._determine_write_disposition(copy_to_reference))

    if not self.bq_io_metadata:
      self.bq_io_metadata = create_bigquery_io_metadata(self._step_name)

    project_id = (
        copy_to_reference.projectId
        if self.load_job_project_id is None else self.load_job_project_id)
    copy_job_name = '%s_%s' % (
        job_name_prefix,
        _bq_uuid(
            '%s:%s.%s' % (
                copy_from_reference.projectId,
                copy_from_reference.datasetId,
                copy_from_reference.tableId)))
    job_reference = self.bq_wrapper._insert_copy_job(
        project_id,
        copy_job_name,
        copy_from_reference,
        copy_to_reference,
        create_disposition=self.create_disposition,
        write_disposition=write_disposition,
        job_labels=self.bq_io_metadata.add_additional_bq_job_labels())

    if wait_for_job:
      self.bq_wrapper.wait_for_bq_job(job_reference, sleep_duration_sec=10)
    self.pending_jobs.append(
        GlobalWindows.windowed_value((destination, job_reference)))

  def _determine_write_disposition(self, copy_to_reference) -> tuple[bool, str]:
    """
    Determines the write disposition for a BigQuery copy job,
     based on destination.

    When the write_disposition for a job is WRITE_TRUNCATE, multiple copy jobs
    to the same destination can interfere with each other, truncate data, and
    write to the BigQuery table repeatedly. To prevent this, the first copy job
    runs with the user's specified write_disposition, but subsequent jobs must
    always use WRITE_APPEND. This ensures that subsequent copy jobs do not
    clear out data appended by previous jobs.

    Args:
        copy_to_reference: The reference to the destination table.

    Returns:
        A tuple containing a boolean indicating whether to wait for the job to
        complete and the write disposition to use for the job.
    """
    full_table_ref = '%s:%s.%s' % (
        copy_to_reference.projectId,
        copy_to_reference.datasetId,
        copy_to_reference.tableId)
    if full_table_ref not in self._observed_tables:
      write_disposition = self.write_disposition
      wait_for_job = True
      self._observed_tables.add(full_table_ref)
      Lineage.sinks().add(
          'bigquery',
          copy_to_reference.projectId,
          copy_to_reference.datasetId,
          copy_to_reference.tableId)
    else:
      wait_for_job = False
      write_disposition = 'WRITE_APPEND'
    return wait_for_job, write_disposition

  def finish_bundle(self):
    for windowed_value in self.pending_jobs:
      job_ref = windowed_value.value[1]
      self.bq_wrapper.wait_for_bq_job(
          job_ref, sleep_duration_sec=_SLEEP_DURATION_BETWEEN_POLLS)
      yield windowed_value

    yield pvalue.TaggedOutput(
        TriggerCopyJobs.TRIGGER_DELETE_TEMP_TABLES,
        GlobalWindows.windowed_value(None))


class TriggerLoadJobs(beam.DoFn):
  """Triggers the import jobs to BQ.

  Experimental; no backwards compatibility guarantees.
  """

  TEMP_TABLES = 'TemporaryTables'
  ONGOING_JOBS = 'OngoingJobs'

  def __init__(
      self,
      schema=None,
      project=None,
      create_disposition=None,
      write_disposition=None,
      test_client=None,
      temporary_tables=False,
      additional_bq_parameters=None,
      source_format=None,
      step_name=None,
      load_job_project_id=None):
    self.schema = schema
    self.project = project
    self.test_client = test_client
    self.temporary_tables = temporary_tables
    self.additional_bq_parameters = additional_bq_parameters or {}
    self.source_format = source_format
    self.bq_io_metadata = None
    self._step_name = step_name
    self.load_job_project_id = load_job_project_id
    if self.temporary_tables:
      # If we are loading into temporary tables, we rely on the default create
      # and write dispositions, which mean that a new table will be created.
      self.create_disposition = None
      self.write_disposition = None
    else:
      self.create_disposition = create_disposition
      self.write_disposition = write_disposition

  def display_data(self):
    result = {
        'create_disposition': str(self.create_disposition),
        'write_disposition': str(self.write_disposition),
        'additional_bq_params': str(self.additional_bq_parameters),
        'schema': str(self.schema),
        'launchesBigQueryJobs': DisplayDataItem(
            True, label="This Dataflow job launches bigquery jobs."),
        'source_format': str(self.source_format),
    }
    return result

  def start_bundle(self):
    self.bq_wrapper = bigquery_tools.BigQueryWrapper(client=self.test_client)
    if not self.bq_io_metadata:
      self.bq_io_metadata = create_bigquery_io_metadata(self._step_name)
    self.pending_jobs = []
    self.schema_cache = {}

  def process(
      self,
      element,
      load_job_name_prefix,
      pane_info=beam.DoFn.PaneInfoParam,
      *schema_side_inputs):
    # Each load job is assumed to have files respecting these constraints:
    # 1. Total size of all files < 15 TB (Max size for load jobs)
    # 2. Total no. of files in a single load job < 10,000
    # This assumption means that there will always be a single load job
    # triggered for each partition of files.
    destination = element[0]
    partition_key, files = element[1]

    if callable(self.schema):
      schema = self.schema(destination, *schema_side_inputs)
    elif isinstance(self.schema, vp.ValueProvider):
      schema = self.schema.get()
    else:
      schema = self.schema

    if callable(self.additional_bq_parameters):
      additional_parameters = self.additional_bq_parameters(destination)
    elif isinstance(self.additional_bq_parameters, vp.ValueProvider):
      additional_parameters = self.additional_bq_parameters.get()
    else:
      additional_parameters = self.additional_bq_parameters

    table_reference = bigquery_tools.parse_table_reference(destination)
    if table_reference.projectId is None:
      table_reference.projectId = vp.RuntimeValueProvider.get_value(
          'project', str, '') or self.project
    # Load jobs for a single destination are always triggered from the same
    # worker. This means that we can generate a deterministic numbered job id,
    # and not need to worry.
    destination_hash = _bq_uuid(
        '%s:%s.%s' % (
            table_reference.projectId,
            table_reference.datasetId,
            table_reference.tableId))
    job_name = '%s_%s_pane%s_partition%s' % (
        load_job_name_prefix, destination_hash, pane_info.index, partition_key)
    _LOGGER.info('Load job has %s files. Job name is %s.', len(files), job_name)

    create_disposition = self.create_disposition
    if self.temporary_tables:
      # we need to create temp tables, so we need a schema.
      # if there is no input schema, fetch the destination table's schema
      if schema is None:
        hashed_dest = bigquery_tools.get_hashable_destination(table_reference)
        if hashed_dest in self.schema_cache:
          schema = self.schema_cache[hashed_dest]
        else:
          try:
            schema = bigquery_tools.table_schema_to_dict(
                bigquery_tools.BigQueryWrapper().get_table(
                    project_id=table_reference.projectId,
                    dataset_id=table_reference.datasetId,
                    table_id=table_reference.tableId).schema)
            self.schema_cache[hashed_dest] = schema
          except Exception as e:
            _LOGGER.warning(
                "Input schema is absent and could not fetch the final "
                "destination table's schema [%s]. Creating temp table [%s] "
                "will likely fail: %s",
                hashed_dest,
                job_name,
                e)

      # If we are using temporary tables, then we must always create the
      # temporary tables, so we replace the create_disposition.
      create_disposition = 'CREATE_IF_NEEDED'
      # For temporary tables, we create a new table with the name with JobId.
      table_reference.tableId = job_name
      yield pvalue.TaggedOutput(
          TriggerLoadJobs.TEMP_TABLES,
          bigquery_tools.get_hashable_destination(table_reference))
    else:
      Lineage.sinks().add(
          'bigquery',
          table_reference.projectId,
          table_reference.datasetId,
          table_reference.tableId)

    _LOGGER.info(
        'Triggering job %s to load data to BigQuery table %s.'
        'Schema: %s. Additional parameters: %s. Source format: %s',
        job_name,
        table_reference,
        schema,
        additional_parameters,
        self.source_format,
    )
    if not self.bq_io_metadata:
      self.bq_io_metadata = create_bigquery_io_metadata(self._step_name)

    job_reference = self.bq_wrapper.perform_load_job(
        destination=table_reference,
        source_uris=files,
        job_id=job_name,
        schema=schema,
        write_disposition=self.write_disposition,
        create_disposition=create_disposition,
        additional_load_parameters=additional_parameters,
        source_format=self.source_format,
        job_labels=self.bq_io_metadata.add_additional_bq_job_labels(),
        load_job_project_id=self.load_job_project_id)
    yield pvalue.TaggedOutput(
        TriggerLoadJobs.ONGOING_JOBS, (destination, job_reference))
    self.pending_jobs.append(
        GlobalWindows.windowed_value((destination, job_reference)))

  def finish_bundle(self):
    for windowed_value in self.pending_jobs:
      job_ref = windowed_value.value[1]
      self.bq_wrapper.wait_for_bq_job(
          job_ref, sleep_duration_sec=_SLEEP_DURATION_BETWEEN_POLLS)
    return self.pending_jobs


class PartitionFiles(beam.DoFn):

  MULTIPLE_PARTITIONS_TAG = 'MULTIPLE_PARTITIONS'
  SINGLE_PARTITION_TAG = 'SINGLE_PARTITION'

  class Partition(object):
    def __init__(self, max_size, max_files, files=None, size=0):
      self.max_size = max_size
      self.max_files = max_files
      self.files = files if files is not None else []
      self.size = size

    def can_accept(self, file_size, no_of_files=1):
      if (((self.size + file_size) <= self.max_size) and
          ((len(self.files) + no_of_files) <= self.max_files)):
        return True
      else:
        return False

    def add(self, file_path, file_size):
      self.files.append(file_path)
      self.size += file_size

  def __init__(self, max_partition_size, max_files_per_partition):
    self.max_partition_size = max_partition_size
    self.max_files_per_partition = max_files_per_partition

  def process(self, element):
    destination = element[0]
    files = element[1]
    partitions = []

    if not files:
      _LOGGER.warning(
          'Ignoring a BigQuery batch load partition to %s '
          'that contains no source URIs.',
          destination)
      return

    latest_partition = PartitionFiles.Partition(
        self.max_partition_size, self.max_files_per_partition)

    for file_path, file_size in files:
      if latest_partition.can_accept(file_size):
        latest_partition.add(file_path, file_size)
      else:
        partitions.append(latest_partition.files)
        latest_partition = PartitionFiles.Partition(
            self.max_partition_size, self.max_files_per_partition)
        latest_partition.add(file_path, file_size)
    partitions.append(latest_partition.files)

    if len(partitions) > 1:
      output_tag = PartitionFiles.MULTIPLE_PARTITIONS_TAG
    else:
      output_tag = PartitionFiles.SINGLE_PARTITION_TAG

    # we also pass along the index of partition as a key, which is used
    # to create a deterministic load job name
    for key, partition in enumerate(partitions):
      yield pvalue.TaggedOutput(output_tag, (destination, (key, partition)))


class DeleteTablesFn(beam.DoFn):
  def __init__(self, test_client=None):
    self.test_client = test_client

  def start_bundle(self):
    self.bq_wrapper = bigquery_tools.BigQueryWrapper(client=self.test_client)

  def process(self, table_reference):
    _LOGGER.info("Deleting table %s", table_reference)
    table_reference = bigquery_tools.parse_table_reference(table_reference)
    self.bq_wrapper._delete_table(
        table_reference.projectId,
        table_reference.datasetId,
        table_reference.tableId)


class BigQueryBatchFileLoads(beam.PTransform):
  """Takes in a set of elements, and inserts them to BigQuery via batch loads.

  """

  DESTINATION_JOBID_PAIRS = 'destination_load_jobid_pairs'
  DESTINATION_FILE_PAIRS = 'destination_file_pairs'
  DESTINATION_COPY_JOBID_PAIRS = 'destination_copy_jobid_pairs'
  COUNT = 0

  def __init__(
      self,
      destination,
      project=None,
      schema=None,
      custom_gcs_temp_location=None,
      create_disposition=None,
      write_disposition=None,
      triggering_frequency=None,
      with_auto_sharding=False,
      temp_file_format=None,
      max_file_size=None,
      max_files_per_bundle=None,
      max_partition_size=None,
      max_files_per_partition=None,
      additional_bq_parameters=None,
      table_side_inputs=None,
      schema_side_inputs=None,
      test_client=None,
      validate=True,
      is_streaming_pipeline=False,
      load_job_project_id=None):
    self.destination = destination
    self.project = project
    self.create_disposition = create_disposition
    self.write_disposition = write_disposition
    self.triggering_frequency = triggering_frequency
    self.with_auto_sharding = with_auto_sharding
    self.max_file_size = max_file_size or _DEFAULT_MAX_FILE_SIZE
    self.max_files_per_bundle = (
        max_files_per_bundle or _DEFAULT_MAX_WRITERS_PER_BUNDLE)
    self.max_partition_size = max_partition_size or _MAXIMUM_LOAD_SIZE
    self.max_files_per_partition = (
        max_files_per_partition or _MAXIMUM_SOURCE_URIS)
    if (isinstance(custom_gcs_temp_location, str) or
        custom_gcs_temp_location is None):
      self._custom_gcs_temp_location = vp.StaticValueProvider(
          str, custom_gcs_temp_location or '')
    elif isinstance(custom_gcs_temp_location, vp.ValueProvider):
      self._custom_gcs_temp_location = custom_gcs_temp_location
    else:
      raise ValueError('custom_gcs_temp_location must be str or ValueProvider')

    self.test_client = test_client
    self.schema = schema
    self._temp_file_format = temp_file_format or bigquery_tools.FileFormat.JSON

    # If we have multiple destinations, then we will have multiple load jobs,
    # thus we will need temporary tables for atomicity.
    self.dynamic_destinations = bool(callable(destination))

    self.additional_bq_parameters = additional_bq_parameters or {}
    self.table_side_inputs = table_side_inputs or ()
    self.schema_side_inputs = schema_side_inputs or ()

    self.is_streaming_pipeline = is_streaming_pipeline
    self.load_job_project_id = load_job_project_id
    self._validate = validate
    if self._validate:
      self.verify()

  def verify(self):
    if (isinstance(self._custom_gcs_temp_location.get(), vp.StaticValueProvider)
        and not self._custom_gcs_temp_location.get().startswith('gs://')):
      # Only fail if the custom location is provided, and it is not a GCS
      # location.
      raise ValueError(
          'Invalid GCS location: %r.\n'
          'Writing to BigQuery with FILE_LOADS method requires a '
          'GCS location to be provided to write files to be '
          'loaded into BigQuery. Please provide a GCS bucket, or '
          'pass method="STREAMING_INSERTS" to WriteToBigQuery.' %
          self._custom_gcs_temp_location.get())
    if self.is_streaming_pipeline and not self.triggering_frequency:
      raise ValueError(
          'triggering_frequency must be specified to use file'
          'loads in streaming')
    elif not self.is_streaming_pipeline and self.triggering_frequency:
      raise ValueError(
          'triggering_frequency can only be used with file'
          'loads in streaming')
    if not self.is_streaming_pipeline and self.with_auto_sharding:
      return ValueError(
          'with_auto_sharding can only be used with file loads in streaming.')

  def _window_fn(self):
    """Set the correct WindowInto PTransform"""

    # The user-supplied triggering_frequency is often chosen to control how
    # many BigQuery load jobs are triggered, to prevent going over BigQuery's
    # daily quota for load jobs. If this is set to a large value, currently we
    # have to buffer all the data until the trigger fires. Instead we ensure
    # that the files are written if a threshold number of records are ready.
    # We use only the user-supplied trigger on the actual BigQuery load.
    # This allows us to offload the data to the filesystem.
    #
    # In the case of dynamic sharding, however, we use a default trigger since
    # the transform performs sharding also batches elements to avoid generating
    # too many tiny files. User trigger is applied right after writes to limit
    # the number of load jobs.
    if self.is_streaming_pipeline and not self.with_auto_sharding:
      return beam.WindowInto(beam.window.GlobalWindows(),
                             trigger=trigger.Repeatedly(
                                 trigger.AfterAny(
                                     trigger.AfterProcessingTime(
                                         self.triggering_frequency),
                                     trigger.AfterCount(
                                         _FILE_TRIGGERING_RECORD_COUNT))),
                             accumulation_mode=trigger.AccumulationMode\
                                 .DISCARDING)
    else:
      return beam.WindowInto(beam.window.GlobalWindows())

  def _maybe_apply_user_trigger(self, destination_file_kv_pc):
    if self.is_streaming_pipeline:
      # Apply the user's trigger back before we start triggering load jobs
      return (
          destination_file_kv_pc
          | "ApplyUserTrigger" >> beam.WindowInto(
              beam.window.GlobalWindows(),
              trigger=trigger.Repeatedly(
                  trigger.AfterAll(
                      trigger.AfterProcessingTime(self.triggering_frequency),
                      trigger.AfterCount(1))),
              accumulation_mode=trigger.AccumulationMode.DISCARDING))
    else:
      return destination_file_kv_pc

  def _write_files(self, destination_data_kv_pc, file_prefix_pcv):
    outputs = (
        destination_data_kv_pc
        | beam.ParDo(
            WriteRecordsToFile(
                schema=self.schema,
                max_files_per_bundle=self.max_files_per_bundle,
                max_file_size=self.max_file_size,
                file_format=self._temp_file_format),
            file_prefix_pcv,
            *self.schema_side_inputs).with_outputs(
                WriteRecordsToFile.UNWRITTEN_RECORD_TAG,
                WriteRecordsToFile.WRITTEN_FILE_TAG))

    # A PCollection of (destination, file) tuples. It lists files with records,
    # and the destination each file is meant to be imported into.
    destination_files_kv_pc = outputs[WriteRecordsToFile.WRITTEN_FILE_TAG]

    # A PCollection of (destination, record) tuples. These are later sharded,
    # grouped, and all records for each destination-shard is written to files.
    # This PCollection is necessary because not all records can be written into
    # files in ``WriteRecordsToFile``.
    unwritten_records_pc = outputs[WriteRecordsToFile.UNWRITTEN_RECORD_TAG]

    more_destination_files_kv_pc = (
        unwritten_records_pc
        | beam.ParDo(_ShardDestinations())
        | "GroupShardedRows" >> beam.GroupByKey()
        | "DropShardNumber" >> beam.Map(lambda x: (x[0][0], x[1]))
        | "WriteGroupedRecordsToFile" >> beam.ParDo(
            WriteGroupedRecordsToFile(
                schema=self.schema, file_format=self._temp_file_format),
            file_prefix_pcv,
            *self.schema_side_inputs))

    # TODO(https://github.com/apache/beam/issues/20285): Remove the identity
    # transform. We flatten both PCollection paths and use an identity function
    # to work around a flatten optimization issue where the wrong coder is
    # being used.
    all_destination_file_pairs_pc = (
        (destination_files_kv_pc, more_destination_files_kv_pc)
        | "DestinationFilesUnion" >> beam.Flatten()
        | "IdentityWorkaround" >> beam.Map(lambda x: x))
    return self._maybe_apply_user_trigger(all_destination_file_pairs_pc)

  def _write_files_with_auto_sharding(
      self, destination_data_kv_pc, file_prefix_pcv):
    clock = self.test_client.test_clock if self.test_client else time.time

    # Auto-sharding is achieved via GroupIntoBatches.WithShardedKey
    # transform which shards, groups and at the same time batches the table rows
    # to be inserted to BigQuery.

    # Firstly, the keys of tagged_data (table references) are converted to a
    # hashable format. This is needed to work with the keyed states used by.
    # GroupIntoBatches. After grouping and batching is done, table references
    # are restored.
    destination_files_kv_pc = (
        destination_data_kv_pc
        |
        'ToHashableTableRef' >> beam.Map(bigquery_tools.to_hashable_table_ref)
        | 'WithAutoSharding' >> util.GroupIntoBatches.WithShardedKey(
            batch_size=_FILE_TRIGGERING_RECORD_COUNT,
            max_buffering_duration_secs=_FILE_TRIGGERING_BATCHING_DURATION_SECS,
            clock=clock)
        | 'FromHashableTableRefAndDropShard' >> beam.Map(
            lambda kvs:
            (bigquery_tools.parse_table_reference(kvs[0].key), kvs[1]))
        | beam.ParDo(
            WriteGroupedRecordsToFile(
                schema=self.schema, file_format=self._temp_file_format),
            file_prefix_pcv,
            *self.schema_side_inputs))

    return self._maybe_apply_user_trigger(destination_files_kv_pc)

  def _load_data(
      self,
      partitions_using_temp_tables,
      partitions_direct_to_destination,
      load_job_name_pcv,
      schema_mod_job_name_pcv,
      copy_job_name_pcv,
      p,
      step_name):
    """Load data to BigQuery

    Data is loaded into BigQuery in the following two ways:
      1. Single partition:
         When there is a single partition of files destined to a single
         destination, a single load job is triggered.
      2. Multiple partitions and/or Dynamic Destinations:
         When there are multiple partitions of files destined for a single
         destination or when Dynamic Destinations are used, multiple load jobs
         need to be triggered for each partition/destination. Load Jobs are
         triggered to temporary tables, and those are later copied to the actual
         appropriate destination table. This ensures atomicity when only some
         of the load jobs would fail but not other. If any of them fails, then
         copy jobs are not triggered.
    """
    self.reshuffle_before_load = not util.is_compat_version_prior_to(
        p.options, "2.65.0")
    if self.reshuffle_before_load:
      # Ensure that TriggerLoadJob retry inputs are deterministic by breaking
      # fusion for inputs.
      partitions_using_temp_tables = (
          partitions_using_temp_tables
          | "ReshuffleBeforeLoadWithTempTables" >> beam.Reshuffle())
      partitions_direct_to_destination = (
          partitions_direct_to_destination
          | "ReshuffleBeforeLoadWithoutTempTables" >> beam.Reshuffle())

    # Load data using temp tables
    trigger_loads_outputs = (
        partitions_using_temp_tables
        | "TriggerLoadJobsWithTempTables" >> beam.ParDo(
            TriggerLoadJobs(
                schema=self.schema,
                project=self.project,
                write_disposition=self.write_disposition,
                create_disposition=self.create_disposition,
                test_client=self.test_client,
                temporary_tables=True,
                additional_bq_parameters=self.additional_bq_parameters,
                source_format=self._temp_file_format,
                step_name=step_name,
                load_job_project_id=self.load_job_project_id),
            load_job_name_pcv,
            *self.schema_side_inputs).with_outputs(
                TriggerLoadJobs.TEMP_TABLES,
                TriggerLoadJobs.ONGOING_JOBS,
                main='main'))

    finished_temp_tables_load_job_ids_pc = trigger_loads_outputs['main']
    temp_tables_load_job_ids_pc = trigger_loads_outputs[
        TriggerLoadJobs.ONGOING_JOBS]
    temp_tables_pc = trigger_loads_outputs[TriggerLoadJobs.TEMP_TABLES]

    schema_mod_job_ids_pc = (
        finished_temp_tables_load_job_ids_pc
        | beam.ParDo(
            UpdateDestinationSchema(
                project=self.project,
                write_disposition=self.write_disposition,
                test_client=self.test_client,
                additional_bq_parameters=self.additional_bq_parameters,
                step_name=step_name,
                load_job_project_id=self.load_job_project_id),
            schema_mod_job_name_pcv))

    if self.write_disposition in ('WRITE_EMPTY', 'WRITE_TRUNCATE'):
      # All loads going to the same table must be processed together so that
      # the truncation happens only once. See
      # https://github.com/apache/beam/issues/24535.
      finished_temp_tables_load_job_ids_list_pc = (
          finished_temp_tables_load_job_ids_pc | beam.MapTuple(
              lambda destination, job_reference: (
                  bigquery_tools.parse_table_reference(destination).tableId,
                  (destination, job_reference)))
          | beam.GroupByKey()
          | beam.MapTuple(lambda tableId, batch: list(batch)))
    else:
      # Loads can happen in parallel.
      finished_temp_tables_load_job_ids_list_pc = (
          finished_temp_tables_load_job_ids_pc
          # This name is to ensure update compat.
          | "Map(<lambda at bigquery_file_loads.py:1157>)" >>
          beam.Map(lambda x: [x]))

    copy_job_outputs = (
        finished_temp_tables_load_job_ids_list_pc
        | beam.ParDo(
            TriggerCopyJobs(
                project=self.project,
                create_disposition=self.create_disposition,
                write_disposition=self.write_disposition,
                test_client=self.test_client,
                step_name=step_name,
                load_job_project_id=self.load_job_project_id),
            copy_job_name_pcv,
            pvalue.AsIter(schema_mod_job_ids_pc)).with_outputs(
                TriggerCopyJobs.TRIGGER_DELETE_TEMP_TABLES, main='main'))

    destination_copy_job_ids_pc = copy_job_outputs['main']
    trigger_delete = copy_job_outputs[
        TriggerCopyJobs.TRIGGER_DELETE_TEMP_TABLES]

    _ = (
        temp_tables_pc
        | "RemoveTempTables/AddUselessValue" >> beam.Map(
            lambda x, unused_trigger: (x, None), pvalue.AsList(trigger_delete))
        | "RemoveTempTables/DeduplicateTables" >> beam.GroupByKey()
        | "RemoveTempTables/GetTableNames" >> beam.Keys()
        | "RemoveTempTables/Delete" >> beam.ParDo(
            DeleteTablesFn(self.test_client)))

    # Load data directly to destination table
    destination_load_job_ids_pc = (
        partitions_direct_to_destination
        | "TriggerLoadJobsWithoutTempTables" >> beam.ParDo(
            TriggerLoadJobs(
                schema=self.schema,
                write_disposition=self.write_disposition,
                create_disposition=self.create_disposition,
                test_client=self.test_client,
                temporary_tables=False,
                additional_bq_parameters=self.additional_bq_parameters,
                source_format=self._temp_file_format,
                step_name=step_name,
                load_job_project_id=self.load_job_project_id),
            load_job_name_pcv,
            *self.schema_side_inputs).with_outputs(
                TriggerLoadJobs.ONGOING_JOBS, main='main')
    )[TriggerLoadJobs.ONGOING_JOBS]

    destination_load_job_ids_pc = (
        (temp_tables_load_job_ids_pc, destination_load_job_ids_pc)
        | beam.Flatten())

    return destination_load_job_ids_pc, destination_copy_job_ids_pc

  def expand(self, pcoll):
    p = pcoll.pipeline
    self.project = self.project or p.options.view_as(GoogleCloudOptions).project
    try:
      step_name = self.label
    except AttributeError:
      step_name = 'BigQueryBatchFileLoads_%d' % BigQueryBatchFileLoads.COUNT
      BigQueryBatchFileLoads.COUNT += 1

    temp_location = p.options.view_as(GoogleCloudOptions).temp_location
    job_name = (
        p.options.view_as(GoogleCloudOptions).job_name or 'AUTOMATIC_JOB_NAME')

    empty_pc = p | "ImpulseEmptyPC" >> beam.Create([])
    singleton_pc = p | "ImpulseSingleElementPC" >> beam.Create([None])

    load_job_name_pcv = pvalue.AsSingleton(
        singleton_pc
        | "LoadJobNamePrefix" >> beam.Map(
            lambda _: _generate_job_name(
                job_name, bigquery_tools.BigQueryJobTypes.LOAD, 'LOAD_STEP')))

    schema_mod_job_name_pcv = pvalue.AsSingleton(
        singleton_pc
        | "SchemaModJobNamePrefix" >> beam.Map(
            lambda _: _generate_job_name(
                job_name, bigquery_tools.BigQueryJobTypes.LOAD,
                'SCHEMA_MOD_STEP')))

    copy_job_name_pcv = pvalue.AsSingleton(
        singleton_pc
        | "CopyJobNamePrefix" >> beam.Map(
            lambda _: _generate_job_name(
                job_name, bigquery_tools.BigQueryJobTypes.COPY, 'COPY_STEP')))

    file_prefix_pcv = pvalue.AsSingleton(
        singleton_pc
        | "GenerateFilePrefix" >> beam.Map(
            file_prefix_generator(
                self._validate, self._custom_gcs_temp_location, temp_location)))

    destination_data_kv_pc = (
        pcoll
        | "RewindowIntoGlobal" >> self._window_fn()
        | "AppendDestination" >> beam.ParDo(
            bigquery_tools.AppendDestinationsFn(self.destination),
            *self.table_side_inputs))

    if not self.with_auto_sharding:
      all_destination_file_pairs_pc = self._write_files(
          destination_data_kv_pc, file_prefix_pcv)
    else:
      all_destination_file_pairs_pc = self._write_files_with_auto_sharding(
          destination_data_kv_pc, file_prefix_pcv)

    grouped_files_pc = (
        all_destination_file_pairs_pc
        | "GroupFilesByTableDestinations" >> beam.GroupByKey())

    partitions = (
        grouped_files_pc
        | beam.ParDo(
            PartitionFiles(
                self.max_partition_size,
                self.max_files_per_partition)).with_outputs(
                    PartitionFiles.MULTIPLE_PARTITIONS_TAG,
                    PartitionFiles.SINGLE_PARTITION_TAG))

    multiple_partitions_per_destination_pc = partitions[
        PartitionFiles.MULTIPLE_PARTITIONS_TAG]
    single_partition_per_destination_pc = partitions[
        PartitionFiles.SINGLE_PARTITION_TAG]

    # When using dynamic destinations, elements with both single as well as
    # multiple partitions are loaded into BigQuery using temporary tables to
    # ensure atomicity.
    if self.dynamic_destinations:
      all_partitions = ((
          multiple_partitions_per_destination_pc,
          single_partition_per_destination_pc)
                        | "FlattenPartitions" >> beam.Flatten())
      destination_load_job_ids_pc, destination_copy_job_ids_pc = (
          self._load_data(all_partitions,
                          empty_pc,
                          load_job_name_pcv,
                          schema_mod_job_name_pcv,
                          copy_job_name_pcv,
                          p,
                          step_name))
    else:
      destination_load_job_ids_pc, destination_copy_job_ids_pc = (
          self._load_data(multiple_partitions_per_destination_pc,
                          single_partition_per_destination_pc,
                          load_job_name_pcv,
                          schema_mod_job_name_pcv,
                          copy_job_name_pcv,
                          p,
                          step_name))

    return {
        self.DESTINATION_JOBID_PAIRS: destination_load_job_ids_pc,
        self.DESTINATION_FILE_PAIRS: all_destination_file_pairs_pc,
        self.DESTINATION_COPY_JOBID_PAIRS: destination_copy_job_ids_pc,
    }
