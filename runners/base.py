"""Minimal test-runner interface used by run_suite.py.

A runner is responsible only for discovering tests and describing how to
execute one selected test. Everything else (DynamoRIO wrapping, per-PID
trace collection, timeouts, resume behavior, logging, and summary
generation) stays generic in run_suite.py.
"""


class TestRunner:
    def discover_tests(self):
        """Return a list of test dicts: {"name": str, "helpers": [str, ...]}.

        A runner may attach extra keys to each dict for its own use in
        command_for_test()/working_dir_for_test(), but "name" and "helpers"
        must always be present.
        """
        raise NotImplementedError

    def command_for_test(self, test):
        """Return the argv list (list[str]) that runs `test` directly.

        This must be the real invocation of the test executable itself,
        not a wrapper (such as a separate test-driver process) that would
        prevent DynamoRIO from instrumenting the code under test.
        """
        raise NotImplementedError

    def working_dir_for_test(self, test):
        """Optional per-test working directory override.

        Return None (the default) to use run_suite.py's --cwd. Runners
        such as CTest, where individual tests may have been registered
        with their own working directory, can override this.
        """
        return None
