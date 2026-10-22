"""Compose 一次性建表/迁移任务；不插入演示业务数据。"""

from db.init_db import create_tables
from db.migrate_agent_trace import migrate_agent_trace
from db.migrate_conversation_scope import migrate_conversation_scope
from db.migrate_action_receipts import migrate_action_receipts
from db.migrate_task_execution import migrate_task_execution
from db.migrate_task_recovery import migrate_task_recovery


def main():
    create_tables()
    migrate_agent_trace()
    migrate_conversation_scope()
    migrate_action_receipts()
    migrate_task_execution()
    migrate_task_recovery()


if __name__ == "__main__":
    main()
