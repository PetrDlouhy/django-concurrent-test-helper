from __future__ import print_function

import json
import sys
import warnings
from contextlib import contextmanager
from functools import partial
from importlib import import_module
from optparse import make_option

from django.core.management.base import BaseCommand, CommandError
from django.db import connections, DEFAULT_DB_ALIAS
from django.test.simple import dependency_ordered
from django.test.utils import setup_test_environment

from ... import b64pickle


@contextmanager
def redirect_stdout(to):
    original = sys.stdout
    sys.stdout = to
    yield
    sys.stdout = original


def use_test_databases():
    """
    Adapted from DjangoTestSuiteRunner.setup_databases
    """

    # First pass -- work out which databases connections need to be switched
    # and which ones are test mirrors or duplicate entries in DATABASES
    mirrored_aliases = {}
    test_databases = {}
    dependencies = {}
    for alias in connections:
        connection = connections[alias]
        if connection.settings_dict['TEST_MIRROR']:
            # If the database is marked as a test mirror, save
            # the alias.
            mirrored_aliases[alias] = (
                connection.settings_dict['TEST_MIRROR'])
        else:
            # Store a tuple with DB parameters that uniquely identify it.
            # If we have two aliases with the same values for that tuple,
            # they will have the same test db name.
            item = test_databases.setdefault(
                connection.creation.test_db_signature(),
                (connection.settings_dict['NAME'], [])
            )
            item[1].append(alias)

            if 'TEST_DEPENDENCIES' in connection.settings_dict:
                dependencies[alias] = (
                    connection.settings_dict['TEST_DEPENDENCIES'])
            else:
                if alias != DEFAULT_DB_ALIAS:
                    dependencies[alias] = connection.settings_dict.get(
                        'TEST_DEPENDENCIES', [DEFAULT_DB_ALIAS])

    # Second pass -- switch the databases to use test db settings.
    for signature, (db_name, aliases) in dependency_ordered(
            test_databases.items(), dependencies):
        # get test db name from the first connection
        connection = connections[aliases[0]]
        for alias in aliases:
            connection = connections[alias]
            test_db_name = connection.creation._get_test_db_name()
            if test_db_name == ':memory:':
                # Django converts all sqlite test dbs to :memory: ...but
                # they can't be shared between concurrent processes...
                # in this case it also means our parent test run used an
                # in-memory db that we can't share
                warnings.warn(
                    "In-memory databases can't be shared between concurrent test processes."
                )
            # we are running late in Django life-cycle so it has already
            # opened connections to default db, need to close and re-open
            # against test db:
            connection.close()
            connection.settings_dict['NAME'] = test_db_name
            connection.cursor()

    for alias, mirror_alias in mirrored_aliases.items():
        # we are running late in Django life-cycle so it has already
        # opened connections to default db, need to close and re-open
        # against test mirror db:
        connection = connections[alias]
        connection.close()
        connection.settings_dict['NAME'] = (
            connections[mirror_alias].settings_dict['NAME'])
        connection.features = connections[mirror_alias].features
        connection.cursor()


class Command(BaseCommand):
    """
    The goal of this command is to allow us to do actual concurrent requests
    in a test case.

    It seems kind of cumbersome to run our function via a manage.py command.
    It would be nicer to just use multiprocessing and call our function that
    way. However, multiprocessing under Python 2 on Unix always uses os.fork
    ...and the forked processes inherit sockets, such as postgres db, but in a
    broken state. I didn't find a way to successfully fork a Django process
    and no-one on SO did etiher.

    So the idea is for the parent test case to set up concurrent calls to this
    command via subprocess (e.g. via multiprocessing.Pool)

    You don't need to use this command directly, see `dp_utils.concurrent_tests`
    for helper functions.
    """

    option_list = BaseCommand.option_list + (
        make_option(
            '-k', '--kwargs',
            help='kwargs to request client method call (serialized to ascii)',
        ),
        make_option(
            '-s', '--serializer',
            help='Serialization format',
            type='choice', choices=('b64pickle', 'json'),
            default='b64pickle',
            # json is included to have a hand-editable option, which may be
            # useful if running this command directly (dev use only)
        ),
        make_option(
            '-t', '--no-test-db',
            help="Don't patch connection to use test db",
            action='store_true',
        ),
        # (dev use only) if running this command directly, option to use the
        # default dbs created via syndb instead of dbs from parent test run
    )

    help = "We use nosetests path format - path.to.module:function_name"

    def handle(self, *args, **kwargs):
        if not args:
            raise CommandError(
                'Must supply an import path to function to execute')

        module_name, function_name = args[0].split(':')
        module = import_module(module_name)
        f = getattr(module, function_name)

        serializer_name = kwargs['serializer']
        if serializer_name == 'b64pickle':
            serialize = b64pickle.dumps
            deserialize = b64pickle.loads
        elif serializer_name == 'json':
            serialize = partial(json.dumps, ensure_ascii=True)
            deserialize = json.loads

        f_kwargs = deserialize(kwargs['kwargs'] or '{}')

        # redirect any printing that `f` may do so as not to pollute our
        # output (which is deserialized as return value by caller)
        with redirect_stdout(sys.stderr):
            setup_test_environment()
            # ensure we're using test dbs, shared with parent test run
            if not kwargs['no_test_db']:
                use_test_databases()
            result = f(**f_kwargs)

        print(serialize(result), end='')