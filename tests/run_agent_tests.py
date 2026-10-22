"""统一执行 Agent 自动化回归；默认不连接 MySQL。"""

import sys
import unittest


TEST_MODULES = (
    "test_confirmation_p0",
    "test_agent_step_trace",
    "tests.test_agent_flow",
    "tests.test_infrastructure",
    "tests.test_auth_jwt",
    "tests.test_llm_compatibility",
    "tests.test_agent_routing",
    "tests.test_policy_routing",
    "tests.test_policy_admin",
    "tests.test_frontend",
)


def main():
    suite = unittest.defaultTestLoader.loadTestsFromNames(TEST_MODULES)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
