import argparse
import importlib
import os
import subprocess
import sys
from argparse import Namespace
from datetime import datetime
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import Tuple, Iterator
import importlib.util
import bigflow as bf
from typing import Optional
from glob import glob1

import bigflow

from bigflow import Config
from bigflow.deploy import deploy_dags_folder, deploy_docker_image, load_image_from_tar
from bigflow.resources import find_file
from bigflow.scaffold import start_project
from bigflow.version import get_version, release
from .commons import run_process

SETUP_VALIDATION_MESSAGE = 'BigFlow setup is valid.'


def resolve(path: Path) -> str:
    return str(path.absolute())


def walk_module_files(root_package: Path) -> Iterator[Tuple[str, str]]:
    """
    Returning all the Python files in the `root_package`

    Example:
    walk_module_files(Path("fld")) -> [("/path/to/fld", "file1"), ("/path/to/fld", "file2")]

    @return: (absolute_path: str, name: str)
    """
    resolved_root_package = resolve(root_package)
    for subdir, dirs, files in os.walk(resolved_root_package):
        for file in files:
            if file.endswith('.py'):
                yield subdir, file


def build_module_path(root_package: Path, module_dir: Path, module_file: str) -> str:
    """
    Returns module path that can be imported using `import_module`
    """
    full_module_file_path = resolve(module_dir / module_file)
    full_module_file_path = full_module_file_path.replace(resolve(root_package.parent), '')
    return full_module_file_path \
               .replace(os.sep, '.')[1:] \
        .replace('.py', '') \
        .replace('.__init__', '')


def walk_module_paths(root_package: Path) -> Iterator[str]:
    """
    Returning all the module paths in the `root_package`
    """
    for module_dir, module_file in walk_module_files(root_package):
        yield build_module_path(root_package, Path(module_dir), module_file)


def walk_modules(root_package: Path) -> Iterator[ModuleType]:
    """
    Imports all the modules in the path and returns
    """
    for module_path in walk_module_paths(root_package):
        try:
            yield import_module(module_path)
        except ValueError as e:
            print(f"Skipping module {module_path}. Can't import due to exception {str(e)}.")


def walk_module_objects(module: ModuleType, expect_type: type) -> Iterator[Tuple[str, type]]:
    """
    Returns module items of the set type
    """
    for name, obj in module.__dict__.items():
        if isinstance(obj, expect_type):
            yield name, obj


def walk_workflows(root_package: Path) -> Iterator[bf.Workflow]:
    """
    Imports modules in the `root_package` and returns all the elements of the type bf.Workflow
    """
    for module in walk_modules(root_package):
        for name, workflow in walk_module_objects(module, bf.Workflow):
            yield workflow


def find_workflow(root_package: Path, workflow_id: str) -> bf.Workflow:
    """
    Imports modules and finds the workflow with id workflow_id
    """
    for workflow in walk_workflows(root_package):
        if workflow.workflow_id == workflow_id:
            return workflow
    raise ValueError('Workflow with id {} not found in package {}'.format(workflow_id, root_package))


def set_configuration_env(env):
    """
    Sets 'bf_env' env variable
    """
    if env is not None:
        os.environ['bf_env'] = env
        print(f"bf_env is : {os.environ.get('bf_env', None)}")


def _init_workflow_log(workflow: bf.Workflow):
    if not workflow.log_config:
        return

    try:
        import bigflow.log
    except ImportError:
        # `log` extras is not installed?
        pass
    else:
        bigflow.log.init_workflow_logging(workflow)


def execute_job(root_package: Path, workflow_id: str, job_id: str, runtime=None):
    """
    Executes the job with the `workflow_id`, with job id `job_id`

    @param runtime: str determine partition that will be used for write operations.
    """
    w = find_workflow(root_package, workflow_id)
    _init_workflow_log(w)
    w.run_job(job_id, runtime)


def execute_workflow(root_package: Path, workflow_id: str, runtime=None):
    """
    Executes the workflow with the `workflow_id`

    @param runtime: str determine partition that will be used for write operations.
    """
    w = find_workflow(root_package, workflow_id)
    _init_workflow_log(w)
    w.run(runtime)


def read_project_name_from_setup() -> Optional[str]:
    try:
        sys.path.insert(1, os.getcwd())
        import project_setup
        return project_setup.PROJECT_NAME
    except Exception:
        return None


def build_project_name_description(project_name: str) -> str:
    if project_name is None:
        return ''
    else:
        return 'Project name is taken from project_setup.PROJECT_NAME: {0}.'.format(project_name)


def find_root_package(project_name: Optional[str], project_dir: Optional[str]) -> Path:
    """
    Finds project package path. Tries first to find location in project_setup.PROJECT_NAME,
    and if not found then by making a path to the `root` module

    @param project_dir: Path to the root package of a project, used only when PROJECT_NAME not set
    @return: Path
    """
    if project_name is not None:
        return Path(project_name)
    else:
        root_module = import_module(project_dir)
        return Path(root_module.__file__.replace('__init__.py', ''))


def import_deployment_config(deployment_config_path: str, property_name: str):
    if not Path(deployment_config_path).exists():
        raise ValueError(f"Can't find deployment_config.py at '{deployment_config_path}'. "
                         f"Property '{property_name}' can't be resolved. "
                          "If your deployment_config.py is elswhere, "
                          "you can set path to it using --deployment-config-path. If you are not using deployment_config.py -- "
                         f"set '{property_name}' property as a command line argument.")
    spec = importlib.util.spec_from_file_location('deployment_config', deployment_config_path)

    if not spec:
        raise ValueError(f'Failed to load deployment_config from {deployment_config_path}. '
        'Create a proper deployment_config.py file'
        'or set all the properties via command line arguments.')

    deployment_config_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(deployment_config_module)

    if not isinstance(deployment_config_module.deployment_config, Config):
        raise ValueError('deployment_config attribute in deployment_config.py should be instance of bigflow.Config')

    return deployment_config_module.deployment_config


def cli_run(project_package: str,
            runtime: Optional[str] = None,
            full_job_id: Optional[str] = None,
            workflow_id: Optional[str] = None) -> None:
    """
    Runs the specified job or workflow

    @param project_package: str The main package of a user's project
    @param runtime: Optional[str] Date of XXX in format "%Y-%m-%d %H:%M:%S"
    @param full_job_id: Optional[str] Represents both workflow_id and job_id in a string in format "<workflow_id>.<job_id>"
    @param workflow_id: Optional[str] The id of the workflow that should be executed
    @return:
    """
    if full_job_id is not None:
        try:
            workflow_id, job_id = full_job_id.split('.')
        except ValueError:
            raise ValueError(
                'You should specify job using the workflow_id and job_id parameters - --job <workflow_id>.<job_id>.')
        execute_job(project_package, workflow_id, job_id, runtime=runtime)
    elif workflow_id is not None:
        execute_workflow(project_package, workflow_id, runtime=runtime)
    else:
        raise ValueError('You must provide the --job or --workflow for the run command.')


def _parse_args(project_name: Optional[str], args) -> Namespace:
    parser = argparse.ArgumentParser(description=f'Welcome to BigFlow CLI.'
                                                  '\nType: bigflow {command} -h to print detailed help for a selected command.')
    subparsers = parser.add_subparsers(dest='operation',
                                       required=True,
                                       help='BigFlow command to execute')

    _create_run_parser(subparsers, project_name)
    _create_deploy_dags_parser(subparsers)
    _create_deploy_image_parser(subparsers)
    _create_deploy_parser(subparsers)

    _create_build_dags_parser(subparsers)
    _create_build_image_parser(subparsers)
    _create_build_package_parser(subparsers)
    _create_build_parser(subparsers)

    _create_project_version_parser(subparsers)
    _create_release_parser(subparsers)
    _create_start_project_parser(subparsers)

    return parser.parse_args(args)


def _create_start_project_parser(subparsers):
    subparsers.add_parser('start-project', description='Creates a scaffolding project in a current directory.')


def _create_build_parser(subparsers):
    parser = subparsers.add_parser('build', description='Builds a Docker image, DAG files and .whl package from local sources.')
    _add_build_dags_parser_arguments(parser)


def _create_build_package_parser(subparsers):
    subparsers.add_parser('build-package', description='Builds .whl package from local sources.')


def _valid_datetime(dt):
    try:
        datetime.strptime(dt, "%Y-%m-%d %H:%M:%S")
        return dt
    except ValueError:
        try:
            datetime.strptime(dt, "%Y-%m-%d")
            return dt
        except ValueError:
            raise ValueError("Not a valid date: '{0}'.".format(dt))


def _add_build_dags_parser_arguments(parser):
    parser.add_argument('-w', '--workflow',
                        type=str,
                        help="Leave empty to build DAGs from all workflows. "
                             "Set a workflow Id to build selected workflow only. "
                             "For example to build only this workflow: bigflow.Workflow(workflow_id='workflow1',"
                             " definition=[ExampleJob('job1')]) you should use --workflow workflow1")
    parser.add_argument('-t', '--start-time',
                        help='The first runtime of a workflow. '
                             'For workflows triggered hourly -- datetime in format: Y-m-d H:M:S, for example 2020-01-01 00:00:00. '
                             'For workflows triggered daily -- date in format: Y-m-d, for example 2020-01-01. '
                             'If empty, current hour is used for hourly workflows and '
                             'today for daily workflows. ',
                        type=_valid_datetime)


def _create_build_dags_parser(subparsers):
    parser = subparsers.add_parser('build-dags',
                                   description='Builds DAG files from local sources to {current_dir}/.dags')
    _add_build_dags_parser_arguments(parser)


def _create_build_image_parser(subparsers):
    subparsers.add_parser('build-image',
                          description='Builds a docker image from local files.')


def _create_run_parser(subparsers, project_name):
    parser = subparsers.add_parser('run',
                                   description='BigFlow CLI run command -- run a workflow or job')

    group = parser.add_mutually_exclusive_group()
    group.required = True
    group.add_argument('-j', '--job',
                       type=str,
                       help='The job to start, identified by workflow id and job id in format "<workflow_id>.<job_id>".')
    group.add_argument('-w', '--workflow',
                       type=str,
                       help='The id of the workflow to start.')
    parser.add_argument('-r', '--runtime',
                        type=str, default=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        help='The date and time when this job or workflow should be started. '
                             'The default is now (%(default)s). '
                             'Examples: 2019-01-01, 2020-01-01 01:00:00')
    _add_parsers_common_arguments(parser)

    if project_name is None:
        parser.add_argument('--project-package',
                            required=True,
                            type=str,
                            help='The main package of your project. '
                                 'Should contain project_setup.py')


def _add_parsers_common_arguments(parser):
    parser.add_argument('-c', '--config',
                        type=str,
                        help='Config environment name that should be used. For example: dev, prod.'
                             ' If not set, default Config name will be used.'
                             ' This env name is applied to all bigflow.Config objects that are defined by'
                             ' individual workflows as well as to deployment_config.py.')


def _add_deploy_parsers_common_arguments(parser):
    parser.add_argument('-a', '--auth-method',
                        type=str,
                        default='local_account',
                        help='One of two authentication method: '
                             'local_account -- you are using credentials of your local user authenticated in gcloud; '
                             'vault -- credentials for service account are obtained from Vault. '
                             'Default: local_account',
                        choices=['local_account', 'vault'])
    parser.add_argument('-ve', '--vault-endpoint',
                        type=str,
                        help='URL of a Vault endpoint to get OAuth token for service account. '
                             'Required if auth-method is vault. '
                             'If not set, will be read from deployment_config.py.'
                        )
    parser.add_argument('-vs', '--vault-secret',
                        type=str,
                        help='Vault secret token. '
                             'Required if auth-method is vault.'
                        )
    parser.add_argument('-dc', '--deployment-config-path',
                        type=str,
                        help='Path to the deployment_config.py file. '
                             'If not set, {current_dir}/deployment_config.py will be used.')

    _add_parsers_common_arguments(parser)


def _create_deploy_parser(subparsers):
    parser = subparsers.add_parser('deploy',
                                   description='Performs complete deployment. Uploads DAG files from local DAGs folder '
                                               'to Composer and uploads Docker image to Container Registry.')

    _add_deploy_dags_parser_arguments(parser)
    _add_deploy_image_parser_arguments(parser)
    _add_deploy_parsers_common_arguments(parser)


def _create_deploy_image_parser(subparsers):
    parser = subparsers.add_parser('deploy-image',
                                   description='Uploads Docker image to Container Registry.'
                                   )

    _add_deploy_image_parser_arguments(parser)
    _add_deploy_parsers_common_arguments(parser)


def _create_deploy_dags_parser(subparsers):
    parser = subparsers.add_parser('deploy-dags',
                                   description='Uploads DAG files from local DAGs folder to Composer.')

    _add_deploy_dags_parser_arguments(parser)
    _add_deploy_parsers_common_arguments(parser)


def _create_project_version_parser(subparsers):
    subparsers.add_parser('project-version', aliases=['pv'], description='Prints project version')


def _create_release_parser(subparsers):
    parser = subparsers.add_parser('release', description='Creates a new release tag')
    parser.add_argument('-i', '--ssh-identity-file',
                        type=str,
                        help="Path to the identity file, used to authorize push to remote repository"
                             " If not specified, default ssh configuration will be used.")


def _add_deploy_image_parser_arguments(parser):
    parser.add_argument('-i', '--image-tar-path',
                        type=str,
                        help='Path to a Docker image file. The file name must contain version number with the following naming schema: image-{version}.tar')
    parser.add_argument('-r', '--docker-repository',
                        type=str,
                        help='Name of a local and target Docker repository. Typically, a target repository is hosted by Google Cloud Container Registry.'
                             ' If so, with the following naming schema: {HOSTNAME}/{PROJECT-ID}/{IMAGE}.'
                        )

def _add_deploy_dags_parser_arguments(parser):
    parser.add_argument('-dd', '--dags-dir',
                        type=str,
                        help="Path to the folder with DAGs to deploy."
                             " If not set, {current_dir}/.dags will be used.")
    parser.add_argument('-cdf', '--clear-dags-folder',
                        action='store_true',
                        help="Clears the DAGs bucket before uploading fresh DAG files. "
                             "Default: False")

    parser.add_argument('-p', '--gcp-project-id',
                        help="Name of your Google Cloud Platform project."
                             " If not set, will be read from deployment_config.py")

    parser.add_argument('-b', '--dags-bucket',
                        help="Name of the target Google Cloud Storage bucket which underlies DAGs folder of your Composer."
                             " If not set, will be read from deployment_config.py")


def read_project_package(args):
    return args.project_package if hasattr(args, 'project_package') else None


def _resolve_deployment_config_path(args):
    if args.deployment_config_path:
        return args.deployment_config_path
    return os.path.join(os.getcwd(), 'deployment_config.py')


def _resolve_dags_dir(args):
    if args.dags_dir:
        return args.dags_dir
    return os.path.join(os.getcwd(), '.dags')


def _resolve_vault_endpoint(args):
    if args.auth_method == 'vault':
        return _resolve_property(args, 'vault_endpoint')
    else:
        return None


def _resolve_property(args, property_name):
    cli_atr = getattr(args, property_name)
    if cli_atr:
        return cli_atr
    else:
        config = import_deployment_config(_resolve_deployment_config_path(args), property_name)
        return config.resolve_property(property_name, args.config)


def _cli_deploy_dags(args):
    try:
        vault_secret = _resolve_property(args, 'vault_secret')
    except ValueError:
        vault_secret = None
    deploy_dags_folder(dags_dir=_resolve_dags_dir(args),
                       dags_bucket=_resolve_property(args, 'dags_bucket'),
                       clear_dags_folder=args.clear_dags_folder,
                       auth_method=args.auth_method,
                       vault_endpoint=_resolve_vault_endpoint(args),
                       vault_secret=vault_secret,
                       project_id=_resolve_property(args, 'gcp_project_id')
                       )


def _load_image_from_tar(image_tar_path: str):
    print(f'Loading Docker image from {image_tar_path} ...', )


def _cli_deploy_image(args):
    docker_repository = _resolve_property(args, 'docker_repository')
    try:
        vault_secret = _resolve_property(args, 'vault_secret')
    except ValueError:
        vault_secret = None
    image_tar_path = args.image_tar_path if args.image_tar_path  else find_image_file()

    deploy_docker_image(image_tar_path=image_tar_path,
                        auth_method=args.auth_method,
                        docker_repository=docker_repository,
                        vault_endpoint=_resolve_vault_endpoint(args),
                        vault_secret=vault_secret)


def find_image_file():
    files = glob1("image", "*-*.tar")
    if files:
        return os.path.join("image", files[0])
    else:
        raise ValueError('File containing image to deploy not found')


def _cli_build_image(args):
    validate_project_setup()
    cmd = 'python project_setup.py build_project --build-image'
    run_process(cmd)


def _cli_build_package():
    validate_project_setup()
    cmd = 'python project_setup.py build_project --build-package'
    run_process(cmd)


def _cli_build_dags(args):
    validate_project_setup()
    cmd = ['python', 'project_setup.py', 'build_project', '--build-dags']
    if args.workflow:
        cmd.append('--workflow')
        cmd.append(args.workflow)
    if args.start_time:
        cmd.append('--start-time')
        cmd.append(args.start_time)
    run_process(cmd)


def _cli_build(args):
    validate_project_setup()
    cmd = ['python', 'project_setup.py', 'build_project']
    if args.workflow:
        cmd.append('--workflow')
        cmd.append(args.workflow)
    if args.start_time:
        cmd.append('--start-time')
        cmd.append(args.start_time)
    run_process(cmd)


def project_type_input():
    project_type = input("Would you like to create basic or advanced project? Default basic. Type 'a' for advanced.\n")
    return project_type if project_type else 'b'


def project_number_input():
    project_number = input('How many GCP projects would you like to use? '
                           'It allows to deploy your workflows to more than one project. Default 2\n')
    return project_number if project_number else '2'


def gcloud_project_list():
    return subprocess.getoutput('gcloud projects list')


def get_default_project_from_gcloud():
    return subprocess.getoutput('gcloud config get-value project')


def project_id_input(n):
    if n == 0:
        project = input(f'Enter a GCP project ID that you are going to use in your BigFlow project. '
                        f'Choose a project from the list above. '
                        f'If not provided default project: {get_default_project_from_gcloud()} will be used.\n')
    else:
        project = input(f'Enter a #{n} GCP project ID that you are going to use in your BigFlow project. '
                        f'Choose a project from the list above.'
                        f' If not provided default project: {get_default_project_from_gcloud()} will be used.\n')
    return project


def gcp_project_flow(n):
    projects_list = gcloud_project_list()
    print(projects_list)
    return gcp_project_input(n, projects_list)


def gcp_project_input(n, projects_list):
    project = project_id_input(n)
    if project == '':
        return get_default_project_from_gcloud()
    if project not in projects_list:
        print(f'You do not have access to {project}. Try another project from the list.\n')
        return gcp_project_input(n, projects_list)
    return project


def gcp_bucket_input():
    return input('Enter a Cloud Composer Bucket name where DAG files will be stored.\n')


def environment_name_input(envs):
    environment_name = input('Enter an environment name. Default dev\n')
    if environment_name in envs:
        print(f'Environment with name{environment_name} is already defined. Try another name.\n')
        return environment_name_input(envs)
    return environment_name if environment_name else 'dev'


def project_name_input():
    return input('Enter the project name. It should be valid python package name. '
                 'It will be used as a main directory of your project and bucket name used by dataflow to run jobs.\n')


def _cli_start_project():
    config = {'is_basic': False, 'project_name': project_name_input(), 'projects_id': [], 'composers_bucket': [], 'envs': []}
    if False:
        for n in range(0, int(project_number_input())):
            config['projects_id'].append(gcp_project_flow(n))
            config['composers_bucket'].append(gcp_bucket_input())
            config['envs'].append(environment_name_input(config['envs']))
    else:
        config['is_basic'] = True
        config['projects_id'].append(gcp_project_flow(0))
        config['composers_bucket'].append(gcp_bucket_input())
    start_project(config)
    print('Bigflow project created successfully.')


def check_if_project_setup_exists():
    find_file('project_setup.py', Path('.'), 1)


def validate_project_setup():
    check_if_project_setup_exists()
    cmd = ['python', 'project_setup.py', 'build_project', '--validate-project-setup']
    output = run_process(cmd)
    if SETUP_VALIDATION_MESSAGE not in output:
        raise ValueError('The project_setup.py is invalid. Check the documentation how to create a valid project_setup.py: https://github.com/allegro/bigflow/blob/master/docs/build.md')


def _cli_project_version(args):
    print(get_version())


def _cli_release(args):
    release(args.ssh_identity_file)


def cli(raw_args) -> None:
    project_name = read_project_name_from_setup()
    parsed_args = _parse_args(project_name, raw_args)
    operation = parsed_args.operation

    if operation == 'run':
        set_configuration_env(parsed_args.config)
        root_package = find_root_package(project_name, read_project_package(parsed_args))
        cli_run(root_package, parsed_args.runtime, parsed_args.job, parsed_args.workflow)
    elif operation == 'deploy-image':
        _cli_deploy_image(parsed_args)
    elif operation == 'deploy-dags':
        _cli_deploy_dags(parsed_args)
    elif operation == 'deploy':
        _cli_deploy_image(parsed_args)
        _cli_deploy_dags(parsed_args)
    elif operation == 'build-dags':
        _cli_build_dags(parsed_args)
    elif operation == 'build-image':
        _cli_build_image(parsed_args)
    elif operation == 'build-package':
        _cli_build_package()
    elif operation == 'build':
        _cli_build(parsed_args)
    elif operation == 'start-project':
        _cli_start_project()
    elif operation == 'project-version' or operation == 'pv':
        _cli_project_version(parsed_args)
    elif operation == 'release':
        _cli_release(parsed_args)
    else:
        raise ValueError(f'Operation unknown - {operation}')
