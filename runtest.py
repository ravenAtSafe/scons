#!/usr/bin/env python
#
# Copyright The SCons Foundation
#
# runtest.py - wrapper script for running SCons tests
#
# The SCons test suite consists of:
#
#  - unit tests        - included in *Tests.py files from SCons/ dir
#  - end-to-end tests  - these are *.py files in test/ directory that
#                        require custom SCons framework from testing/
#
# This script adds SCons/ and testing/ directories to PYTHONPATH,
# performs test discovery and processes them according to options.

"""
Options:
  -a --all                 Run all tests.
  -b --baseline BASE       Run test scripts against baseline BASE.
  -d --debug               Run test scripts under the Python debugger.
  -D --devmode             Run tests in Python's development mode (3.7+ only)
     --e2e-only            Run only the end-to-end tests
  -e --external            Run the script in external mode (for external Tools)
  -f --file FILE           Only run tests listed in FILE.
  -j --jobs JOBS           Run tests in JOBS parallel jobs.
  -k --no-progress         Suppress count and percent progress messages.
  -l --list                List available tests and exit.
  -n --no-exec             No execute, just print command lines.
     --nopipefiles         Do not use the "file pipe" workaround for Popen()
                           for starting tests. WARNING: use only when too much
                           file traffic is giving you trouble AND you can be
                           sure that none of your tests create output >65K
                           chars! You might run into some deadlocks else.
  -o --output FILE         Save the output from a test run to the log file.
  -P PYTHON                Use the specified Python interpreter.
     --passed              Summarize which tests passed.
  -q --quiet               Don't print the test being executed.
     --quit-on-failure     Quit on any test failure.
     --runner CLASS        Alternative test runner class for unit tests.
  -s --short-progress      Short progress, prints only the command line.
                           and a percentage value, based on the total and
                           current number of tests.
  -t --time                Print test execution time.
     --unit-only           Run only the unit tests
  -v VERSION               Specify the SCons version.
     --verbose=LEVEL       Set verbose level:
                             1 = print executed commands,
                             2 = print commands and non-zero output,
                             3 = print commands and all output.
  -X                       Test script is executable, don't feed to Python.
  -x --exec SCRIPT         Test SCRIPT.
     --xml file            Save results to file in SCons XML format.
     --exclude-list FILE   List of tests to exclude in the current selection.
                           Use to exclude tests when using the -a option.

Environment Variables:
  PRESERVE, PRESERVE_{PASS,FAIL,NO_RESULT}: preserve test subdirs
  TESTCMD_VERBOSE: turn on verbosity in TestCommand
"""

import getopt
import glob
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
from abc import ABC, abstractmethod
from optparse import OptionParser, BadOptionError
from queue import Queue

cwd = os.getcwd()

baseline = None
external = 0
devmode = False
debug = ''
execute_tests = True
jobs = 1
list_only = False
printcommand = True
print_passed_summary = False
scons = None
scons_exec = False
testlistfile = None
version = ''
print_times = False
python = None
print_progress = True
catch_output = False
suppress_output = False
allow_pipe_files = True
quit_on_failure = False
excludelistfile = None
e2e_only = unit_only = False

script = sys.argv[0].split("/")[-1]
usagestr = """\
Usage: %(script)s [OPTIONS] [TEST ...]
       %(script)s -h|--help
""" % locals()

helpstr = usagestr + __doc__

# "Pass-through" option parsing -- an OptionParser that ignores
# unknown options and lets them pile up in the leftover argument
# list.  Useful to gradually port getopt to optparse.

class PassThroughOptionParser(OptionParser):
    def _process_long_opt(self, rargs, values):
        try:
            OptionParser._process_long_opt(self, rargs, values)
        except BadOptionError as err:
            self.largs.append(err.opt_str)
    def _process_short_opts(self, rargs, values):
        try:
            OptionParser._process_short_opts(self, rargs, values)
        except BadOptionError as err:
            self.largs.append(err.opt_str)

parser = PassThroughOptionParser(add_help_option=False)
parser.add_option('-a', '--all', action='store_true', help="Run all tests.")
parser.add_option('-o', '--output',
                  help="Save the output from a test run to the log file.")
parser.add_option('--runner', metavar='class',
                  help="Test runner class for unit tests.")
parser.add_option('--xml', help="Save results to file in SCons XML format.")
(options, args) = parser.parse_args()

# print("options:", options)
# print("args:", args)


opts, args = getopt.getopt(
    args,
    "b:dDef:hj:klnP:p:qsv:Xx:t",
    [
        "baseline=",
        "debug",
        "devmode", 
        "external",
        "file=",
        "help",
        "no-progress",
        "jobs=",
        "list",
        "no-exec",
        "nopipefiles",
        "passed",
        "python=",
        "quiet",
        "quit-on-failure",
        "short-progress",
        "time",
        "version=",
        "exec=",
        "verbose=",
        "exclude-list=",
        "e2e-only",
        "unit-only",
    ],
)

for o, a in opts:
    if o in ['-b', '--baseline']:
        baseline = a
    elif o in ['-d', '--debug']:
        for d in sys.path:
            pdb = os.path.join(d, 'pdb.py')
            if os.path.exists(pdb):
                debug = pdb
                break
    elif o in ['-D', '--devmode']:
        devmode = True
    elif o in ['-e', '--external']:
        external = True
    elif o in ['-f', '--file']:
        if not os.path.isabs(a):
            a = os.path.join(cwd, a)
        testlistfile = a
    elif o in ['-h', '--help']:
        print(helpstr)
        sys.exit(0)
    elif o in ['-j', '--jobs']:
        jobs = int(a)
        # don't let tests write stdout/stderr directly if multi-job,
        # or outputs will interleave and be hard to read
        catch_output = True
    elif o in ['-k', '--no-progress']:
        print_progress = False
    elif o in ['-l', '--list']:
        list_only = True
    elif o in ['-n', '--no-exec']:
        execute_tests = False
    elif o in ['--nopipefiles']:
        allow_pipe_files = False
    elif o in ['--passed']:
        print_passed_summary = True
    elif o in ['-P', '--python']:
        python = a
    elif o in ['-q', '--quiet']:
        printcommand = False
        suppress_output = catch_output = True
    elif o in ['--quit-on-failure']:
        quit_on_failure = True
    elif o in ['-s', '--short-progress']:
        print_progress = True
        suppress_output = catch_output = True
    elif o in ['-t', '--time']:
        print_times = True
    elif o in ['--verbose']:
        os.environ['TESTCMD_VERBOSE'] = a
    elif o in ['-v', '--version']:
        version = a
    elif o in ['-X']:
        scons_exec = True
    elif o in ['-x', '--exec']:
        scons = a
    elif o in ['--exclude-list']:
        excludelistfile = a
    elif o in ['--e2e-only']:
        e2e_only = True
    elif o in ['--unit-only']:
        unit_only = True


class Unbuffered:
    """ class to arrange for stdout/stderr to be unbuffered """
    def __init__(self, file):
        self.file = file
    def write(self, arg):
        self.file.write(arg)
        self.file.flush()
    def __getattr__(self, attr):
        return getattr(self.file, attr)

sys.stdout = Unbuffered(sys.stdout)
sys.stderr = Unbuffered(sys.stderr)

# possible alternative: switch to using print, and:
# print = functools.partial(print, flush)

if options.output:
    logfile = open(options.output, 'w')
    class Tee:
        def __init__(self, openfile, stream):
            self.file = openfile
            self.stream = stream
        def write(self, data):
            self.file.write(data)
            self.stream.write(data)
    sys.stdout = Tee(logfile, sys.stdout)
    sys.stderr = Tee(logfile, sys.stderr)

# --- define helpers ----
if sys.platform in ('win32', 'cygwin'):
    def whereis(file):
        pathext = [''] + os.environ['PATHEXT'].split(os.pathsep)
        for d in os.environ['PATH'].split(os.pathsep):
            f = os.path.join(d, file)
            for ext in pathext:
                fext = f + ext
                if os.path.isfile(fext):
                    return fext
        return None

else:
    def whereis(file):
        for d in os.environ['PATH'].split(os.pathsep):
            f = os.path.join(d, file)
            if os.path.isfile(f):
                try:
                    st = os.stat(f)
                except OSError:
                    continue
                if stat.S_IMODE(st[stat.ST_MODE]) & 0o111:
                    return f
        return None


_ws = re.compile(r'\s')

def escape(s):
    if _ws.search(s):
        s = '"' + s + '"'
    s = s.replace('\\', '\\\\')
    return s


if not catch_output:
    # Without any output suppressed, we let the subprocess
    # write its stuff freely to stdout/stderr.
    def spawn_it(command_args, env):
        cp = subprocess.run(command_args, shell=False, env=env)
        return cp.stdout, cp.stderr, cp.returncode
else:
    # Else, we catch the output of both pipes...
    if allow_pipe_files:
        # The subprocess.Popen() suffers from a well-known
        # problem. Data for stdout/stderr is read into a
        # memory buffer of fixed size, 65K which is not very much.
        # When it fills up, it simply stops letting the child process
        # write to it. The child will then sit and patiently wait to
        # be able to write the rest of its output. Hang!
        # In order to work around this, we follow a suggestion
        # by Anders Pearson in
        #   http://http://thraxil.org/users/anders/posts/2008/03/13/Subprocess-Hanging-PIPE-is-your-enemy/
        # and pass temp file objects to Popen() instead of the ubiquitous
        # subprocess.PIPE.
        def spawn_it(command_args, env):
            # Create temporary files
            tmp_stdout = tempfile.TemporaryFile(mode='w+t')
            tmp_stderr = tempfile.TemporaryFile(mode='w+t')
            # Start subprocess...
            cp = subprocess.run(command_args,
                                stdout=tmp_stdout,
                                stderr=tmp_stderr,
                                shell=False,
                                env=env)

            try:
                # Rewind to start of files
                tmp_stdout.seek(0)
                tmp_stderr.seek(0)
                # Read output
                spawned_stdout = tmp_stdout.read()
                spawned_stderr = tmp_stderr.read()
            finally:
                # Remove temp files by closing them
                tmp_stdout.close()
                tmp_stderr.close()

            # Return values
            return spawned_stderr, spawned_stdout, cp.returncode

    else:
        # We get here only if the user gave the '--nopipefiles'
        # option, meaning the "temp file" approach for
        # subprocess.communicate() above shouldn't be used.
        # He hopefully knows what he's doing, but again we have a
        # potential deadlock situation in the following code:
        #   If the subprocess writes a lot of data to its stderr,
        #   the pipe will fill up (nobody's reading it yet) and the
        #   subprocess will wait for someone to read it.
        #   But the parent process is trying to read from stdin
        #   (but the subprocess isn't writing anything there).
        #   Hence a deadlock.
        # Be dragons here! Better don't use this!
        def spawn_it(command_args, env):
            cp = subprocess.run(command_args,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                shell=False,
                                env=env)
            return cp.stdout, cp.stderr, cp.returncode


class RuntestBase(ABC):
    """ Base class for tests """
    def __init__(self, path, num, spe=None):
        self.path = path
        self.num = num
        self.stdout = self.stderr = self.status = None
        self.abspath = os.path.abspath(path)
        self.command_args = []
        self.command_str = ""
        self.test_time = self.total_time = 0
        if spe:
            for d in spe:
                f = os.path.join(d, path)
                if os.path.isfile(f):
                    self.abspath = f
                    break

    @abstractmethod
    def execute(self):
        pass


class SystemExecutor(RuntestBase):
    """ Test class for tests executed with spawn_it() """
    def execute(self, env):
        self.stderr, self.stdout, s = spawn_it(self.command_args, env)
        self.status = s
        if s < 0 or s > 2:
            sys.stdout.write("Unexpected exit status %d\n" % s)


class PopenExecutor(RuntestBase):
    """ Test class for tests executed with Popen

    A bit of a misnomer as the Popen call is now wrapped
    by calling subprocess.run (behind the covers uses Popen.
    Very similar to SystemExecutor, but uses command_str
    instead of command_args, and doesn't allow for not catching
    the output).
    """
    # For an explanation of the following 'if ... else'
    # and the 'allow_pipe_files' option, please check out the
    # definition of spawn_it() above.
    if allow_pipe_files:
        def execute(self, env):
            # Create temporary files
            tmp_stdout = tempfile.TemporaryFile(mode='w+t')
            tmp_stderr = tempfile.TemporaryFile(mode='w+t')
            # Start subprocess...
            cp = subprocess.run(self.command_str.split(),
                                stdout=tmp_stdout,
                                stderr=tmp_stderr,
                                shell=False,
                                env=env)
            self.status = cp.returncode

            try:
                # Rewind to start of files
                tmp_stdout.seek(0)
                tmp_stderr.seek(0)
                # Read output
                self.stdout = tmp_stdout.read()
                self.stderr = tmp_stderr.read()
            finally:
                # Remove temp files by closing them
                tmp_stdout.close()
                tmp_stderr.close()
    else:
        def execute(self, env):
            cp = subprocess.run(self.command_str.split(),
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                shell=False,
                                env=env)
            self.status = cp.returncode
            self.stdout = cp.stdout
            self.stderr = cp.stderr

class XML(PopenExecutor):
    """ Test class for tests that will output in scons xml """
    @staticmethod
    def header(f):
        f.write('  <results>\n')

    def write(self, f):
        f.write('    <test>\n')
        f.write('      <file_name>%s</file_name>\n' % self.path)
        f.write('      <command_line>%s</command_line>\n' % self.command_str)
        f.write('      <exit_status>%s</exit_status>\n' % self.status)
        f.write('      <stdout>%s</stdout>\n' % self.stdout)
        f.write('      <stderr>%s</stderr>\n' % self.stderr)
        f.write('      <time>%.1f</time>\n' % self.test_time)
        f.write('    </test>\n')

    def footer(self, f):
        f.write('  <time>%.1f</time>\n' % self.total_time)
        f.write('  </results>\n')

if options.xml:
    Test = XML
else:
    Test = SystemExecutor

# --- start processing ---

sd = None
tools_dir = None
ld = None

if not baseline or baseline == '.':
    base = cwd
elif baseline == '-':
    print("This logic used to checkout from svn. It's been removed. If you used this, please let us know on devel mailing list, IRC, or discord server")
    sys.exit(-1)
else:
    base = baseline

scons_runtest_dir = base

if not external:
    scons_script_dir = sd or os.path.join(base, 'scripts')
    scons_tools_dir = tools_dir or os.path.join(base, 'bin')
    scons_lib_dir = ld or base
else:
    scons_script_dir = sd or ''
    scons_tools_dir = tools_dir or ''
    scons_lib_dir = ld or ''

pythonpath_dir = scons_lib_dir

if scons:
    # Let the version of SCons that the -x option pointed to find
    # its own modules.
    os.environ['SCONS'] = scons
elif scons_lib_dir:
    # Because SCons is really aggressive about finding its modules,
    # it sometimes finds SCons modules elsewhere on the system.
    # This forces SCons to use the modules that are being tested.
    os.environ['SCONS_LIB_DIR'] = scons_lib_dir

if scons_exec:
    os.environ['SCONS_EXEC'] = '1'

if external:
    os.environ['SCONS_EXTERNAL_TEST'] = '1'

os.environ['SCONS_RUNTEST_DIR'] = scons_runtest_dir
os.environ['SCONS_SCRIPT_DIR'] = scons_script_dir
os.environ['SCONS_TOOLS_DIR'] = scons_tools_dir
os.environ['SCONS_CWD'] = cwd
os.environ['SCONS_VERSION'] = version

old_pythonpath = os.environ.get('PYTHONPATH')

# Clear _JAVA_OPTIONS which java tools output to stderr when run breaking tests
if '_JAVA_OPTIONS' in os.environ:
    del os.environ['_JAVA_OPTIONS']

# FIXME: the following is necessary to pull in half of the testing
#        harness from $srcdir/etc. Those modules should be transfered
#        to testing/, in which case this manipulation of PYTHONPATH
#        should be able to go away.
pythonpaths = [pythonpath_dir]

scriptpath = os.path.dirname(os.path.realpath(__file__))

# Add path for testing framework to PYTHONPATH
pythonpaths.append(os.path.join(scriptpath, 'testing', 'framework'))


os.environ['PYTHONPATH'] = os.pathsep.join(pythonpaths)

if old_pythonpath:
    os.environ['PYTHONPATH'] = os.environ['PYTHONPATH'] + \
                               os.pathsep + \
                               old_pythonpath


# ---[ test discovery ]------------------------------------
# This section figures which tests to run.
#
# The initial testlist is made by reading from the testlistfile,
# if supplied, or by looking at the test arguments, if supplied,
# or by looking for all test files if the "all" argument is supplied.
# One of the three is required.
#
# Each test path, whichever of the three sources it comes from,
# specifies either a test file or a directory to search for
# SCons tests. SCons code layout assumes that any file under the 'SCons'
# subdirectory that ends with 'Tests.py' is a unit test, and any Python
# script (*.py) under the 'test' subdirectory is an end-to-end test.
# We need to track these because they are invoked differently.
# find_unit_tests and find_e2e_tests are used for this searching.
#
# Note that there are some tests under 'SCons' that *begin* with
# 'test_', but they're packaging and installation tests, not
# functional tests, so we don't execute them by default.  (They can
# still be executed by hand, though).
#
# Test exclusions, if specified, are then applied.

unittests = []
endtests = []


def scanlist(testlist):
    """Process a testlist file"""
    tests = [t.strip() for t in testlist if not t.startswith('#')]
    return [t for t in tests if t]


def find_unit_tests(directory):
    """ Look for unit tests """
    result = []
    for dirpath, dirnames, filenames in os.walk(directory):
        # Skip folders containing a sconstest.skip file
        if 'sconstest.skip' in filenames:
            continue
        for fname in filenames:
            if fname.endswith("Tests.py"):
                result.append(os.path.join(dirpath, fname))
    return sorted(result)


def find_e2e_tests(directory):
    """ Look for end-to-end tests """
    result = []

    for dirpath, dirnames, filenames in os.walk(directory):
        # Skip folders containing a sconstest.skip file
        if 'sconstest.skip' in filenames:
            continue
        try:
            with open(os.path.join(dirpath, ".exclude_tests")) as f:
                excludes = scanlist(f)
        except EnvironmentError:
            excludes = []
        for fname in filenames:
            if fname.endswith(".py") and fname not in excludes:
                result.append(os.path.join(dirpath, fname))
    return sorted(result)


# initial selection:
if testlistfile:
    with open(testlistfile, 'r') as f:
        tests = scanlist(f)
else:
    testpaths = []
    if options.all:
        testpaths = ['SCons', 'test']
    elif args:
        testpaths = args

    for tp in testpaths:
        # Clean up path so it can match startswith's below
        # sys.stderr.write("Changed:%s->"%tp)
        # remove leading ./ or .\
        if tp.startswith('.') and tp[1] in (os.sep, os.altsep):
            tp = tp[2:]
        # tp = os.path.normpath(tp)
        # sys.stderr.write('->%s<-'%tp)
        # sys.stderr.write("to:%s\n"%tp)
        for path in glob.glob(tp):
            if os.path.isdir(path):
                if path.startswith(('SCons', 'testing')):
                    unittests.extend(find_unit_tests(path))
                elif path.startswith('test'):
                    endtests.extend(find_e2e_tests(path))
            else:
                if path.endswith("Tests.py"):
                    unittests.append(path)
                elif path.endswith(".py"):
                    endtests.append(path)
    tests = unittests + endtests

# Remove exclusions:
if e2e_only:
    tests = [t for t in tests if not t.endswith("Tests.py")]
if unit_only:
    tests = [t for t in tests if t.endswith("Tests.py")]
if excludelistfile:
    with open(excludelistfile, 'r') as f:
        excludetests = scanlist(f)
    tests = [t for t in tests if t not in excludetests]

if not tests:
    sys.stderr.write(usagestr + """
runtest: no tests were found.
         Tests can be specified on the command line, read from a file with
         the -f/--file option, or discovered with -a/--all to run all tests.
""")
    sys.exit(1)


# ---[ test processing ]-----------------------------------
tests = [Test(t, n + 1) for n, t in enumerate(tests)]

if list_only:
    for t in tests:
        sys.stdout.write(t.path + "\n")
    sys.exit(0)

if not python:
    if os.name == 'java':
        python = os.path.join(sys.prefix, 'jython')
    else:
        python = sys.executable
os.environ["python_executable"] = python

if print_times:
    def print_time(fmt, tm):
        sys.stdout.write(fmt % tm)
else:
    def print_time(fmt, tm):
        pass

time_func = time.perf_counter
total_start_time = time_func()
total_num_tests = len(tests)


def log_result(t, io_lock=None):
    """ log the result of a test.

    "log" in this case means writing to stdout. Since we might be
    called from from any of several different threads (multi-job run),
    we need to lock access to the log to avoid interleaving. The same
    would apply if output was a file.

    Args:
        t (Test): (completed) testcase instance
        io_lock (threading.lock): (optional) lock to use
    """

    # there is no lock in single-job run, which includes
    # running test/runtest tests from multi-job run, so check.
    if io_lock:
        io_lock.acquire()
    try:
        if suppress_output or catch_output:
            sys.stdout.write(t.headline)
        if not suppress_output:
            if t.stdout:
                print(t.stdout)
            if t.stderr:
                print(t.stderr)
        print_time("Test execution time: %.1f seconds\n", t.test_time)
    finally:
        if io_lock:
            io_lock.release()

    if quit_on_failure and t.status == 1:
        print("Exiting due to error")
        print(t.status)
        sys.exit(1)


def run_test(t, io_lock=None, run_async=True):
    """ Run a testcase.

    Builds the command line to give to execute().
    Also the best place to record some information that will be
    used in output, which in some conditions is printed here.

    Args:
        t (Test): testcase instance
        io_lock (threading.Lock): (optional) lock to use
        run_async (bool): whether to run asynchronously
    """

    t.headline = ""
    command_args = []
    if debug:
        command_args.append(debug)
    if devmode and sys.version_info >= (3, 7, 0):
            command_args.append('-X dev')
    command_args.append(t.path)
    if options.runner and t.path in unittests:
        # For example --runner TestUnit.TAPTestRunner
        command_args.append('--runner ' + options.runner)
    t.command_args = [escape(python)] + command_args
    t.command_str = " ".join(t.command_args)
    if printcommand:
        if print_progress:
            t.headline += "%d/%d (%.2f%s) %s\n" % (
                t.num,
                total_num_tests,
                float(t.num) * 100.0 / float(total_num_tests),
                "%",
                t.command_str,
            )
        else:
            t.headline += t.command_str + "\n"
    if not suppress_output and not catch_output:
        # defer printing the headline until test is done
        sys.stdout.write(t.headline)
    head, _ = os.path.split(t.abspath)
    fixture_dirs = []
    if head:
        fixture_dirs.append(head)
    fixture_dirs.append(os.path.join(scriptpath, 'test', 'fixture'))

    # Set the list of fixture dirs directly in the environment. Just putting
    # it in os.environ and spawning the process is racy. Make it reliable by
    # overriding the environment passed to execute().
    env = dict(os.environ)
    env['FIXTURE_DIRS'] = os.pathsep.join(fixture_dirs)

    test_start_time = time_func()
    if execute_tests:
        t.execute(env)

    t.test_time = time_func() - test_start_time
    log_result(t, io_lock=io_lock)


class RunTest(threading.Thread):
    """ Test Runner class

    One instance will be created for each job thread in multi-job mode
    """
    def __init__(self, queue=None, io_lock=None,
                 group=None, target=None, name=None, args=(), kwargs=None):
        super().__init__(group=group, target=target, name=name)
        self.queue = queue
        self.io_lock = io_lock

    def run(self):
        for t in iter(self.queue.get, None):
            run_test(t, io_lock=self.io_lock, run_async=True)
            self.queue.task_done()

if jobs > 1:
    print("Running tests using %d jobs" % jobs)
    testq = Queue()
    for t in tests:
        testq.put(t)
    testlock = threading.Lock()
    # Start worker threads to consume the queue
    threads = [RunTest(queue=testq, io_lock=testlock) for _ in range(jobs)]
    for t in threads:
        t.daemon = True
        t.start()
    # wait on the queue rather than the individual threads
    testq.join()
else:
    for t in tests:
        run_test(t, io_lock=None, run_async=False)

# --- all tests are complete by the time we get here ---
if tests:
    tests[0].total_time = time_func() - total_start_time
    print_time("Total execution time for all tests: %.1f seconds\n", tests[0].total_time)

passed = [t for t in tests if t.status == 0]
fail = [t for t in tests if t.status == 1]
no_result = [t for t in tests if t.status == 2]

if len(tests) != 1 and execute_tests:
    if passed and print_passed_summary:
        if len(passed) == 1:
            sys.stdout.write("\nPassed the following test:\n")
        else:
            sys.stdout.write("\nPassed the following %d tests:\n" % len(passed))
        paths = [x.path for x in passed]
        sys.stdout.write("\t" + "\n\t".join(paths) + "\n")
    if fail:
        if len(fail) == 1:
            sys.stdout.write("\nFailed the following test:\n")
        else:
            sys.stdout.write("\nFailed the following %d tests:\n" % len(fail))
        paths = [x.path for x in fail]
        sys.stdout.write("\t" + "\n\t".join(paths) + "\n")
    if no_result:
        if len(no_result) == 1:
            sys.stdout.write("\nNO RESULT from the following test:\n")
        else:
            sys.stdout.write("\nNO RESULT from the following %d tests:\n" % len(no_result))
        paths = [x.path for x in no_result]
        sys.stdout.write("\t" + "\n\t".join(paths) + "\n")

if options.xml:
    if options.xml == '-':
        f = sys.stdout
    else:
        f = open(options.xml, 'w')
    tests[0].header(f)
    #f.write("test_result = [\n")
    for t in tests:
        t.write(f)
    tests[0].footer(f)
    #f.write("];\n")
    if options.xml != '-':
        f.close()

if options.output:
    if isinstance(sys.stdout, Tee):
        sys.stdout.file.close()
    if isinstance(sys.stderr, Tee):
        sys.stderr.file.close()

if fail:
    sys.exit(1)
elif no_result:
    # if no fails, but skips were found
    sys.exit(2)
else:
    sys.exit(0)

# Local Variables:
# tab-width:4
# indent-tabs-mode:nil
# End:
# vim: set expandtab tabstop=4 shiftwidth=4:
