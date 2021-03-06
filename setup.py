#!/usr/bin/python
# -*- coding: utf-8 -*-
import setuptools
import os

with open("README.md", "r") as fh:
    long_description = fh.read()

with open(os.path.join('requirements', 'base.txt'), 'r') as base_requirements:
    install_requires = [l.strip() for l in base_requirements.readlines()]

with open(os.path.join('requirements', 'monitoring_extras.txt'), 'r') as monitoring_extras_requirements:
    monitoring_extras_require = [
        l.strip() for l in monitoring_extras_requirements.readlines()]

with open(os.path.join('requirements', 'bigquery_extras.txt'), 'r') as bigquery_extras_requirements:
    bigquery_extras_require = [l.strip() for l in bigquery_extras_requirements.readlines()]

with open(os.path.join('requirements', 'log_extras.txt'), 'r') as log_extras_requirements:
    log_extras_require = [l.strip() for l in log_extras_requirements.readlines()]


with open(os.path.join('bigflow', '_version.py'), 'r') as version_file:
    version_globals = {}
    exec(version_file.read(), version_globals)
    __version__ = version_globals['__version__']


setuptools.setup(
    name="bigflow",
    version=__version__,
    author=u"Chi",
    author_email="chibox-team@allegrogroup.com",
    description="BigQuery client wrapper with clean API",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/allegro/bigflow",
    packages=setuptools.find_packages(exclude=('test', 'e2e')),
    data_files=[
        ('requirements', [
            'requirements/base.txt',
            'requirements/monitoring_extras.txt',
            'requirements/bigquery_extras.txt',
            'requirements/log_extras.txt',
        ]),
    ],
    classifiers=[
        "Programming Language :: Python :: 3",
        "Operating System :: OS Independent",
    ],
    install_requires=install_requires,
    extras_require={
        'monitoring': monitoring_extras_require,
        'bigquery': bigquery_extras_require,
        'log': log_extras_require
    },
    scripts=["scripts/bf", "scripts/bigflow"]
)
