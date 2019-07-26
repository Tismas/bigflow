import uuid
import logging
from google.cloud import bigquery

DEFAULT_LOCATION = 'EU'


def get_partition_from_run_datetime_or_none(run_datetime):
    """
    :param run_datetime: string run datetime in format YYYY-MM-DD HH:mm:ss or YYY-MM-DD
    :return: string partition in format YYYYMMDD
    """
    if run_datetime is not None:
        return run_datetime[:10].replace('-', '')


class TemplatedDatasetManager(object):
    """
    Decorator that resolves table names/all kind of variables.
    """
    def __init__(self,
                 dataset_manager,
                 internal_tables,
                 external_tables,
                 extras,
                 run_datetime):
        self.dataset_manager = dataset_manager
        self.internal_tables = {t: self.create_full_table_id(t) for t in internal_tables}
        self.external_tables = external_tables
        self.extras = extras
        self.run_datetime = run_datetime

    def write_truncate(self, table_name, sql, custom_run_datetime=None):
        return self.write(self.dataset_manager.write_truncate, table_name, sql, custom_run_datetime)

    def write_append(self, table_name, sql, custom_run_datetime=None):
        return self.write(self.dataset_manager.write_append, table_name, sql, custom_run_datetime)

    def write_tmp(self, table_name, sql, custom_run_datetime=None):
        self.internal_tables[table_name] = self.create_table_id(table_name)
        return self.write(self.dataset_manager.write_tmp, table_name, sql, custom_run_datetime)

    def write(self, write_callable, table_name, sql, custom_run_datetime=None):
        table_id = self.create_table_id(table_name)
        return write_callable(table_id, sql.format(**self.template_variables(custom_run_datetime)))

    def create_table_id(self, table_name):
        table_name_without_partition = table_name.split('$')[0]
        return table_name.replace(
            table_name_without_partition,
            self.create_full_table_id(table_name_without_partition))

    def collect(self, sql, custom_run_datetime=None):
        return self.dataset_manager.collect(sql.format(**self.template_variables(custom_run_datetime)))

    def remove_dataset(self):
        return self.dataset_manager.remove_dataset()

    def create_table(self, create_query):
        return self.dataset_manager.create_table(create_query)

    def create_full_table_id(self, table_name):
        return self.dataset_manager.dataset_id + '.' + table_name

    def table_exists(self, table_name):
        return self.dataset_manager.table_exists(table_name)

    def template_variables(self, custom_run_datetime=None):
        result = {}
        result.update(self.internal_tables)
        result.update(self.external_tables)
        result.update(self.extras)
        result['dt'] = custom_run_datetime or self.run_datetime
        return result


class PartitionedDatasetManager(object):
    """
    Interface available for user. Manages partitioning.
    Delegate rest of the tasks to TemplatedDatasetManager and DatasetManager.
    """
    def __init__(self, templated_dataset_manager, partition):
        self._dataset_manager = templated_dataset_manager
        self.partition = partition

    def write_truncate(self, table_name, sql, partitioned=True, custom_run_datetime=None):
        return self._write(
            self._dataset_manager.write_truncate,
            table_name,
            sql,
            partitioned,
            custom_run_datetime)

    def write_append(self, table_name, sql, partitioned=True, custom_run_datetime=None):
        return self._write(
            self._dataset_manager.write_append,
            table_name,
            sql,
            partitioned,
            custom_run_datetime)

    def write_tmp(self, table_name, sql, custom_run_datetime=None):
        return self._write(
            self._dataset_manager.write_tmp,
            table_name,
            sql,
            False,
            custom_run_datetime)

    def collect(self, sql, custom_run_datetime=None):
        return self._dataset_manager.collect(sql, custom_run_datetime)

    def create_table(self, create_query):
        return self._dataset_manager.create_table(create_query)

    @property
    def run_datetime_str(self):
        return self._dataset_manager.run_datetime

    def remove_dataset(self):
        return self._dataset_manager.remove_dataset()

    def _write(self, write_callable, table_name, sql, partitioned, custom_run_datetime=None):
        custom_partition = get_partition_from_run_datetime_or_none(custom_run_datetime)
        if partitioned:
            table_name = table_name + '${partition}'.format(partition=custom_partition or self.partition)
        return write_callable(table_name, sql, custom_run_datetime)

    def _table_exists(self, table_name):
        return self._dataset_manager.table_exists(table_name)


class DatasetManager(object):
    """
    Manages BigQuery IO operations.
    """
    def __init__(self,
                 bigquery_client,
                 dataset,
                 logger):
        self.bigquery_client = bigquery_client
        self.dataset = dataset
        self.dataset_id = dataset.full_dataset_id.replace(':', '.')
        self.logger = logger

    def write_tmp(self, table_id, sql):
        self.write(table_id, sql, 'WRITE_TRUNCATE')

    def write(self, table_id, sql, mode):
        self.logger.info('%s to %s', mode, table_id)
        job_config = bigquery.QueryJobConfig()
        job_config.use_legacy_sql = False
        job_config.allow_large_results = True

        job_config.destination = table_id
        job_config.write_disposition = mode

        job = self.bigquery_client.query(sql, job_config=job_config)
        job.result()

    def write_truncate(self, table_id, sql):
        self.table_exists_or_error(table_id)
        self.write(table_id, sql, 'WRITE_TRUNCATE')

    def write_append(self, table_id, sql):
        self.table_exists_or_error(table_id)
        self.write(table_id, sql, 'WRITE_APPEND')

    def table_exists_or_error(self, table_id):
        table_name = table_id.split('$')[0].split('.')[2]
        if not self.table_exists(table_name):
            raise ValueError('Table {id} does not exist'.format(id=table_id))

    def create_table(self, create_query):
        self.logger.info('CREATE TABLE: %s', create_query)
        job_config = bigquery.QueryJobConfig()
        job_config.use_legacy_sql = False
        job_config.default_dataset = self.dataset

        job = self.bigquery_client.query(
            create_query,
            job_config=job_config)
        job.result()

    def collect(self, sql):
        self.logger.info('COLLECTING DATA: %s', sql)
        q_result = self.bigquery_client.query(sql)
        return list(q_result.result())

    def remove_dataset(self):
        self.bigquery_client.delete_dataset(self.dataset, delete_contents=True, not_found_ok=True)

    def table_exists(self, table_name):
        return self.bigquery_client.query('''
            SELECT count(*) as table_exists
            FROM `{dataset_id}.__TABLES__`
            WHERE table_id='{table_name}'
            '''.format(
                dataset_id=self.dataset_id,
                table_name=table_name)) \
            .result() \
            .to_dataframe()['table_exists'] \
            .iloc[0] > 0


def create_dataset(dataset_name, bigquery_client, location=DEFAULT_LOCATION):
    dataset = bigquery.Dataset('{project_id}.{dataset_name}'.format(
        project_id=bigquery_client.project,
        dataset_name=dataset_name))
    dataset.location = location
    return bigquery_client.create_dataset(dataset, exists_ok=True)


def random_uuid(suffix=''):
    return uuid.uuid1().hex + suffix


def create_dataset_manager(
        project_id,
        runtime,
        dataset_name=None,
        internal_tables=None,
        external_tables=None,
        extras=None,
        credentials=None,
        location=DEFAULT_LOCATION,
        logger=None):
    """
    Dataset manager factory.
    If dataset does not exist then it will also create dataset with given name.

    :param dataset_name: string dataset name(not dataset id). If not provided, dataset_name will be random string.
    :param internal_tables: list of dataset table names that are gonna be available during processing.
    :param external_tables: dict where key is table alias and value is full table ID.
    :param extras: dict with custom parameters that will be available inside templates.
    :param runtime: string determine partition that will be used for write operations.
     runtime is also available inside templates as {dt}.
     runtime can be either full datetime 'YYYY-MM-DD hh:mm:ss' or just date 'YYYY-MM-DD'.
    :param credentials: google.auth.credentials.Credentials instance. If empty, default credentials on the machine will be used.
    :param project_id: string full project id where dataset being processed is available.
    :param location: string location of dataset will be used to create datasets, tables, jobs, etc. EU by default.
    :param logger: custom logger.
    :return: tuple (full dataset ID, dataset manager).
    """
    dataset_name = dataset_name or random_uuid(suffix='_test_case')
    internal_tables = internal_tables or {}
    external_tables = external_tables or {}
    extras = extras or {}
    if logger is None:
        logging.basicConfig(format='%(asctime)s %(levelname)s %(message)s', level=logging.INFO)
        logger = logging.getLogger(__name__)

    client = bigquery.Client(
        project=project_id,
        credentials=credentials,
        location=location)
    dataset = create_dataset(dataset_name, client, location)

    core_dataset_manager = DatasetManager(client, dataset, logger)
    templated_dataset_manager = TemplatedDatasetManager(core_dataset_manager, internal_tables, external_tables, extras, runtime)
    return dataset.full_dataset_id.replace(':', '.'), PartitionedDatasetManager(templated_dataset_manager, get_partition_from_run_datetime_or_none(runtime))
