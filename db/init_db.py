from db.connection import (
    get_server_connection,
    get_connection,
    MYSQL_DATABASE
)


def create_database():
    """
    如果数据库不存在，则自动创建数据库。
    """

    connection = get_server_connection()
    cursor = connection.cursor()

    sql = f"""
    CREATE DATABASE IF NOT EXISTS `{MYSQL_DATABASE}`
    DEFAULT CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci
    """

    cursor.execute(sql)

    connection.commit()

    cursor.close()
    connection.close()


def create_tables():
    """
    创建项目第一版需要的业务表。
    """

    connection = get_connection()
    cursor = connection.cursor()

    # =========================
    # 1. 用户表
    # =========================
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,

        username VARCHAR(100) NOT NULL,

        phone VARCHAR(30),

        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # =========================
    # 2. 订单表
    # =========================
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,

        order_no VARCHAR(64) NOT NULL UNIQUE,

        user_id BIGINT NOT NULL,

        total_amount DECIMAL(10, 2) NOT NULL,

        status VARCHAR(30) NOT NULL,

        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,

        paid_at DATETIME,

        delivered_at DATETIME,

        INDEX idx_orders_user_id (user_id),

        CONSTRAINT fk_orders_user
        FOREIGN KEY (user_id)
        REFERENCES users(id)
    )
    """)

    # =========================
    # 3. 订单商品表
    # =========================
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS order_items (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,

        order_id BIGINT NOT NULL,

        product_name VARCHAR(255) NOT NULL,

        category VARCHAR(100),

        quantity INT NOT NULL DEFAULT 1,

        unit_price DECIMAL(10, 2) NOT NULL,

        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,

        INDEX idx_order_items_order_id (order_id),

        CONSTRAINT fk_order_items_order
        FOREIGN KEY (order_id)
        REFERENCES orders(id)
    )
    """)

    # =========================
    # 4. 售后表
    # =========================
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS after_sales (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,

        after_sale_no VARCHAR(64) NOT NULL UNIQUE,

        idempotency_key VARCHAR(64),

        user_id BIGINT NOT NULL,

        order_id BIGINT NOT NULL,

        order_item_id BIGINT NOT NULL,

        type VARCHAR(30) NOT NULL,

        reason TEXT,

        status VARCHAR(30) NOT NULL,

        refund_amount DECIMAL(10, 2),

        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,

        updated_at DATETIME
            DEFAULT CURRENT_TIMESTAMP
            ON UPDATE CURRENT_TIMESTAMP,

        INDEX idx_after_sales_user_id (user_id),

        INDEX idx_after_sales_order_id (order_id),

        UNIQUE KEY uk_after_sales_idempotency_key (
            idempotency_key
        ),

        CONSTRAINT fk_after_sales_user
            FOREIGN KEY (user_id)
            REFERENCES users(id),

        CONSTRAINT fk_after_sales_order
            FOREIGN KEY (order_id)
            REFERENCES orders(id),

        CONSTRAINT fk_after_sales_order_item
            FOREIGN KEY (order_item_id)
            REFERENCES order_items(id)
    )
    """)

    #状态日志表
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS after_sale_status_logs (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,

        after_sale_id BIGINT NOT NULL,

        from_status VARCHAR(30),

        to_status VARCHAR(30) NOT NULL,

        operator_type VARCHAR(30)
        NOT NULL DEFAULT 'system',

        operator_id VARCHAR(100),

        remark TEXT,

        created_at DATETIME
        DEFAULT CURRENT_TIMESTAMP,

        INDEX idx_after_sale_status_logs_after_sale_id (
            after_sale_id
        ),

        CONSTRAINT fk_after_sale_status_logs_after_sale
        FOREIGN KEY (after_sale_id)
        REFERENCES after_sales(id)
    )
    """)

    # =========================
    # 5. Agent任务表
    # =========================
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS agent_tasks (
            id BIGINT PRIMARY KEY AUTO_INCREMENT,

            task_no VARCHAR(64) NOT NULL UNIQUE,

            user_id BIGINT NOT NULL,

            conversation_id BIGINT NULL,

            task_type VARCHAR(50) NOT NULL,

            goal TEXT NOT NULL,

            status VARCHAR(30) NOT NULL,

            current_step VARCHAR(100),

            context JSON,

            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,

            updated_at DATETIME
            DEFAULT CURRENT_TIMESTAMP
            ON UPDATE CURRENT_TIMESTAMP,

            INDEX idx_agent_tasks_user_id (user_id),

            CONSTRAINT fk_agent_tasks_user
            FOREIGN KEY (user_id)
            REFERENCES users(id)
        )
    """)

    # =========================
    # 6. Agent任务步骤表
    # =========================
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS agent_task_steps (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,

        task_id BIGINT NULL,

        trace_id VARCHAR(64),

        step_name VARCHAR(100) NOT NULL,

        tool_name VARCHAR(100),

        input_json JSON,

        output_json JSON,

        status VARCHAR(30) NOT NULL,

        error_message TEXT,

        latency_ms FLOAT,

        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,

        INDEX idx_agent_task_steps_task_id (task_id),

        INDEX idx_agent_task_steps_trace_id (trace_id),

        CONSTRAINT fk_agent_task_steps_task
        FOREIGN KEY (task_id)
        REFERENCES agent_tasks(id)
    )
    """)

    # ==========================
    # 7. Agent待确认操作表
    # ==========================
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS agent_pending_actions (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,

        confirmation_id VARCHAR(64) NOT NULL UNIQUE,

        user_id BIGINT NOT NULL,

        conversation_id BIGINT NULL,

        task_id BIGINT,

        action_type VARCHAR(64) NOT NULL,

        preview_json JSON NOT NULL,

        status VARCHAR(30) NOT NULL DEFAULT 'pending',

        execution_result_json JSON,

        error_message TEXT,

        confirmed_at DATETIME,

        executed_at DATETIME,

        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,

        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            ON UPDATE CURRENT_TIMESTAMP,

        INDEX idx_agent_pending_actions_user_id (user_id),

        INDEX idx_agent_pending_actions_status (status),

        CONSTRAINT fk_agent_pending_actions_user
            FOREIGN KEY (user_id)
            REFERENCES users(id)
    )
    """)
    # ==========================
    # 8. Agent会话状态表
    # ==========================
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS agent_sessions (

        id BIGINT PRIMARY KEY AUTO_INCREMENT,


        user_id BIGINT NOT NULL,


        # 当前用户正在确认的操作
        current_confirmation_id VARCHAR(64),


        status VARCHAR(30)
            NOT NULL DEFAULT 'active',


        created_at DATETIME
            DEFAULT CURRENT_TIMESTAMP,


        updated_at DATETIME
            DEFAULT CURRENT_TIMESTAMP
            ON UPDATE CURRENT_TIMESTAMP,


        UNIQUE KEY uk_agent_sessions_user_id(
            user_id
        ),


        CONSTRAINT fk_agent_sessions_user

            FOREIGN KEY(user_id)

            REFERENCES users(id)

    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS conversations (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        user_id BIGINT NOT NULL,
        title VARCHAR(255) NOT NULL DEFAULT '新会话',
        status VARCHAR(30) NOT NULL DEFAULT 'active',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        INDEX idx_conversations_user_updated (user_id, updated_at),
        CONSTRAINT fk_conversations_user FOREIGN KEY (user_id) REFERENCES users(id)
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS conversation_messages (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        conversation_id BIGINT NOT NULL,
        role VARCHAR(20) NOT NULL,
        content TEXT NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_conversation_messages_conversation_id (conversation_id, id),
        CONSTRAINT fk_conversation_messages_conversation
            FOREIGN KEY (conversation_id) REFERENCES conversations(id)
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS handoff_task (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        user_id BIGINT NOT NULL,
        conversation_id BIGINT NOT NULL,
        reason TEXT NOT NULL,
        status VARCHAR(30) NOT NULL DEFAULT 'waiting',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_handoff_task_user_id (user_id),
        CONSTRAINT fk_handoff_task_user FOREIGN KEY (user_id) REFERENCES users(id),
        CONSTRAINT fk_handoff_task_conversation
            FOREIGN KEY (conversation_id) REFERENCES conversations(id)
    )
    """)



    connection.commit()

    cursor.close()
    connection.close()


def insert_demo_data():
    """
    插入第一批测试数据。

    如果已经存在测试用户，则不重复插入。
    """

    connection = get_connection()
    cursor = connection.cursor()

    # 先检查测试用户是否已经存在
    cursor.execute("""
    SELECT id
    FROM users
    WHERE phone = %s
    LIMIT 1
    """, ("13800000000",))

    existing_user = cursor.fetchone()

    if existing_user:
        print("测试数据已经存在，跳过插入。")

        cursor.close()
        connection.close()
        return

    # =========================
    # 1. 创建测试用户
    # =========================
    cursor.execute("""
    INSERT INTO users (
        username,
        phone
    )
    VALUES (%s, %s)
    """, (
        "测试用户",
        "13800000000"
    ))

    user_id = cursor.lastrowid

    # =========================
    # 2. 创建测试订单
    # =========================
    cursor.execute("""
    INSERT INTO orders (
        order_no,
        user_id,
        total_amount,
        status,
        paid_at,
        delivered_at
    )
    VALUES (
        %s,
        %s,
        %s,
        %s,
        NOW(),
        NOW()
    )
    """, (
        "ORD202609020001",
        user_id,
        1399.00,
        "delivered"
    ))

    order_id = cursor.lastrowid

    # =========================
    # 3. 创建订单商品
    # =========================
    cursor.execute("""
    INSERT INTO order_items (
        order_id,
        product_name,
        category,
        quantity,
        unit_price
    )
    VALUES (%s, %s, %s, %s, %s)
    """, (
        order_id,
        "AirPods Pro",
        "耳机",
        1,
        1399.00
    ))

    connection.commit()

    cursor.close()
    connection.close()

    print("测试数据插入完成。")


def init_database():
    """
    项目数据库初始化总入口。
    """

    print("开始创建数据库...")

    create_database()

    print("数据库创建完成。")

    print("开始创建数据表...")

    create_tables()

    print("数据表创建完成。")

    print("开始初始化测试数据...")

    insert_demo_data()

    print("数据库初始化全部完成。")


if __name__ == "__main__":
    init_database()
