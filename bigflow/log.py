import logging
import typing
import sys

from textwrap import dedent

from google.cloud import logging_v2
from google.cloud.logging_v2.gapic.enums import LogSeverity
from urllib.parse import quote_plus

try:
    from typing import TypedDict
except ImportError:
    TypedDict = dict


class GCPLoggerHandler(logging.Handler):

    def __init__(self, project_id, log_name, workflow_id):
        logging.StreamHandler.__init__(self)

        self.client = self.create_logging_client()
        self.project_id = project_id
        self.workflow_id = workflow_id
        self.log_name = log_name

        self._log_entry_prototype = logging_v2.types.LogEntry(
            log_name=f"projects/{self.project_id}/logs/{self.log_name}",
            labels={
                "id": str(self.workflow_id or self.project_id),
            },
            resource={
                "type": "global",
                "labels": {
                    "project_id": str(self.project_id),
                },
            },
        )

    def create_logging_client(self):
        return logging_v2.LoggingServiceV2Client()

    def emit(self, record: logging.LogRecord):
        cl_log_level = record.levelname  # CloudLogging list of supported log levels is a superset of python logging level names
        message = self.format(record)
        self.write_log_entries(message, cl_log_level)

    def write_log_entries(self, message, severity):
        entry = logging_v2.types.LogEntry()
        entry.CopyFrom(self._log_entry_prototype)
        # FIXME: maybe 'jsonPayload' ?
        entry.text_payload = message
        entry.severity = LogSeverity[severity]
        self.client.write_log_entries([entry])


def _uncaught_exception_handler(logger):
    def handler(exception_type, value, traceback):
        logger.error(f'Uncaught exception: {value}', exc_info=(exception_type, value, traceback))
    return handler


_LOGGING_CONFIGURED = False


class LogConfigDict(TypedDict):
    gcp_project_id: str
    log_name: str
    level: typing.Union[str, int]


def init_logging(config: LogConfigDict, force=False):

    global _LOGGING_CONFIGURED
    if _LOGGING_CONFIGURED and not force:
        import warnings
        warnings.warn(UserWarning("bigflow.log is is already configured - skip"))
        return

    _LOGGING_CONFIGURED = True

    gcp_project_id = config['gcp_project_id']
    workflow_id = config.get('workflow_id', "unknown-workflow")
    log_name = config.get('log_name', workflow_id)
    log_level = config.get('log_level', 'INFO')

    logging.basicConfig(level=log_level)
    gcp_logger_handler = GCPLoggerHandler(gcp_project_id, log_name, workflow_id)
    gcp_logger_handler.setLevel(logging.INFO)
    # TODO: add formatter?

    query = quote_plus(dedent(f'''
        logName="projects/{gcp_project_id}/logs/{log_name}"
        labels.id="{workflow_id or gcp_project_id}"
    ''').strip())
    logging.info(dedent(f"""
           *************************LOGS LINK*************************
            You can find this workflow logs here: https://console.cloud.google.com/logs/query;query={query}
           ***********************************************************"""))
    logging.getLogger(None).addHandler(gcp_logger_handler)
    sys.excepthook = _uncaught_exception_handler(logging.getLogger('uncaught_exception'))
