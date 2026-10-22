def get_user_by_id(cursor, user_id: int):
    """
    根据用户ID查询用户。
    """

    cursor.execute("""
    SELECT
        id,
        username,
        phone,
        created_at
    FROM users
    WHERE id = %s
    """, (user_id,))

    return cursor.fetchone()


def get_orders_by_user_id(cursor, user_id: int):
    """
    根据用户ID查询该用户的所有订单。
    """

    cursor.execute("""
    SELECT
        id,
        user_id,
        order_no,
        total_amount,
        status,
        created_at,
        paid_at,
        delivered_at
    FROM orders
    WHERE user_id = %s
    ORDER BY created_at DESC
    """, (user_id,))

    return cursor.fetchall()


def get_order_items_by_order_id(cursor, order_id: int):
    """
    根据订单内部ID查询订单商品。
    """

    cursor.execute("""
    SELECT
        id,
        product_name,
        category,
        quantity,
        unit_price
    FROM order_items
    WHERE order_id = %s
    ORDER BY id ASC
    """, (order_id,))

    return cursor.fetchall()

def get_order_items_by_order_ids(
    cursor,
    order_ids: list[int]
):
    """
    根据多个订单ID，一次性查询所有订单商品。
    """

    if not order_ids:
        return []

    placeholders = ",".join(
        ["%s"] * len(order_ids)
    )

    sql = f"""
    SELECT
        id,
        order_id,
        product_name,
        category,
        quantity,
        unit_price
    FROM order_items
    WHERE order_id IN ({placeholders})
    ORDER BY order_id ASC, id ASC
    """

    cursor.execute(
        sql,
        tuple(order_ids)
    )

    return cursor.fetchall()

def get_order_by_order_no_and_user_id(
    cursor,
    user_id: int,
    order_no: str,
    lock_for_share: bool = False,
):
    """
    根据用户ID和订单业务编号查询订单。
    """

    sql = """
    SELECT
        id,
        order_no,
        user_id,
        total_amount,
        status,
        created_at,
        paid_at,
        delivered_at
    FROM orders
    WHERE user_id = %s
      AND order_no = %s
    LIMIT 1
    """
    if lock_for_share:
        sql += " FOR SHARE"
    cursor.execute(sql, (
        user_id,
        order_no
    ))

    return cursor.fetchone()

def get_order_item_by_id_and_order_id(
    cursor,
    order_item_id: int,
    order_id: int,
    for_update: bool = False,
):
    """
    根据订单商品ID和订单ID查询商品。
    用来确认这个商品确实属于这个订单。
    """

    sql = """
    SELECT
        id,
        order_id,
        product_name,
        category,
        quantity,
        unit_price
    FROM order_items
    WHERE id = %s
      AND order_id = %s
    LIMIT 1
    """
    if for_update:
        sql += " FOR UPDATE"
    cursor.execute(sql, (
        order_item_id,
        order_id
    ))

    return cursor.fetchone()


def get_active_after_sale_by_order_item_id(
    cursor,
    order_item_id: int
):
    """
    查询这个订单商品是否已经存在进行中的售后申请。
    """

    cursor.execute("""
    SELECT
        id,
        after_sale_no,
        user_id,
        order_id,
        order_item_id,
        type,
        reason,
        status,
        refund_amount,
        created_at,
        updated_at
    FROM after_sales
    WHERE order_item_id = %s
      AND status IN (
          'pending',
          'processing',
          'approved'
      )
    ORDER BY created_at DESC
    LIMIT 1
    """, (order_item_id,))

    return cursor.fetchone()


def create_after_sale(
    cursor,
    after_sale_no: str,
    user_id: int,
    order_id: int,
    order_item_id: int,
    after_sale_type: str,
    reason: str,
    status: str = "pending",
    refund_amount=None,
    idempotency_key: str | None = None
):
    """
    创建售后申请。
    """

    cursor.execute("""
    INSERT INTO after_sales (
        after_sale_no,
        idempotency_key,
        user_id,
        order_id,
        order_item_id,
        type,
        reason,
        status,
        refund_amount
    )
    VALUES (
        %s,
        %s,
        %s,
        %s,
        %s,
        %s,
        %s,
        %s,
        %s
    )
    """, (
        after_sale_no,
        idempotency_key,
        user_id,
        order_id,
        order_item_id,
        after_sale_type,
        reason,
        status,
        refund_amount
    ))

    return cursor.lastrowid

def get_after_sale_by_id(
    cursor,
    after_sale_id: int
):
    """
    根据售后内部ID查询售后记录。
    """

    cursor.execute("""
    SELECT
        id,
        after_sale_no,
        user_id,
        order_id,
        order_item_id,
        type,
        reason,
        status,
        refund_amount,
        created_at,
        updated_at
    FROM after_sales
    WHERE id = %s
    LIMIT 1
    """, (after_sale_id,))

    return cursor.fetchone()


def update_after_sale_status(
    cursor,
    after_sale_id: int,
    new_status: str
):
    """
    更新售后状态。
    """

    cursor.execute("""
    UPDATE after_sales
    SET status = %s
    WHERE id = %s
    """, (
        new_status,
        after_sale_id
    ))

    return cursor.rowcount

def get_after_sales_by_user_id(
    cursor,
    user_id: int
):
    """
    根据用户ID查询用户全部售后申请，
    同时返回订单编号和商品信息。
    """

    cursor.execute("""
    SELECT
        a.id,
        a.after_sale_no,
        a.user_id,
        a.order_id,
        a.order_item_id,
        a.type,
        a.reason,
        a.status,
        a.refund_amount,
        a.created_at,
        a.updated_at,

        o.order_no,

        oi.product_name,
        oi.category,
        oi.quantity,
        oi.unit_price

    FROM after_sales a

    JOIN orders o
        ON a.order_id = o.id

    JOIN order_items oi
        ON a.order_item_id = oi.id

    WHERE a.user_id = %s

    ORDER BY a.created_at DESC
    """, (user_id,))

    return cursor.fetchall()

def get_after_sale_by_no_and_user_id(
    cursor,
    user_id: int,
    after_sale_no: str
):
    """
    根据用户ID和售后业务编号查询具体售后，
    同时返回订单和商品信息。
    """

    cursor.execute("""
    SELECT
        a.id,
        a.after_sale_no,
        a.user_id,
        a.order_id,
        a.order_item_id,
        a.type,
        a.reason,
        a.status,
        a.refund_amount,
        a.created_at,
        a.updated_at,

        o.order_no,

        oi.product_name,
        oi.category,
        oi.quantity,
        oi.unit_price

    FROM after_sales a

    JOIN orders o
        ON a.order_id = o.id

    JOIN order_items oi
        ON a.order_item_id = oi.id

    WHERE a.user_id = %s
      AND a.after_sale_no = %s

    LIMIT 1
    """, (
        user_id,
        after_sale_no
    ))

    return cursor.fetchone()

def create_after_sale_status_log(
    cursor,
    after_sale_id: int,
    from_status,
    to_status: str,
    operator_type: str = "system",
    operator_id=None,
    remark=None
):
    """
    创建售后状态变化日志。
    """

    cursor.execute("""
    INSERT INTO after_sale_status_logs (
        after_sale_id,
        from_status,
        to_status,
        operator_type,
        operator_id,
        remark
    )
    VALUES (
        %s,
        %s,
        %s,
        %s,
        %s,
        %s
    )
    """, (
        after_sale_id,
        from_status,
        to_status,
        operator_type,
        operator_id,
        remark
    ))

    return cursor.lastrowid

def get_after_sale_status_logs_by_after_sale_id(
    cursor,
    after_sale_id: int
):
    """
    根据售后内部ID查询完整状态变化历史。
    """

    cursor.execute("""
    SELECT
        id,
        after_sale_id,
        from_status,
        to_status,
        operator_type,
        operator_id,
        remark,
        created_at
    FROM after_sale_status_logs
    WHERE after_sale_id = %s
    ORDER BY created_at ASC, id ASC
    """, (after_sale_id,))

    return cursor.fetchall()

def create_agent_pending_action(
    cursor,
    confirmation_id: str,
    user_id: int,
    action_type: str,
    preview_json: str,
    task_id: int | None = None,
    conversation_id: int | None = None,
):
    cursor.execute("""
        INSERT INTO agent_pending_actions (
            confirmation_id,
            user_id,
            conversation_id,
            task_id,
            action_type,
            preview_json,
            status
        )
        VALUES (%s, %s, %s, %s, %s, %s, 'pending')
    """, (
        confirmation_id,
        user_id,
        conversation_id,
        task_id,
        action_type,
        preview_json
    ))

    return cursor.lastrowid


def lock_agent_task_execution_owner(
    cursor,
    task_id: int,
    user_id: int,
    conversation_id: int | None,
    execution_id: str,
) -> bool:
    """Lock and validate the Task owner in the pending-action transaction."""
    sql = """
        SELECT id
        FROM agent_tasks
        WHERE id=%s AND user_id=%s AND execution_id=%s
          AND status='running' AND recovery_id IS NULL
    """
    params: list = [task_id, user_id, execution_id]
    if conversation_id is None:
        sql += " AND conversation_id IS NULL"
    else:
        sql += " AND conversation_id=%s"
        params.append(conversation_id)
    sql += " FOR UPDATE"
    cursor.execute(sql, tuple(params))
    return cursor.fetchone() is not None

def get_agent_pending_action_by_confirmation_id(
    cursor,
    confirmation_id: str,
    user_id: int | None = None,
    conversation_id: int | None = None,
):
    sql = """
        SELECT
            id,
            confirmation_id,
            user_id,
            conversation_id,
            task_id,
            action_type,
            preview_json,
            status,
            execution_result_json,
            created_at,
            updated_at,
            confirmed_at,
            executed_at
        FROM agent_pending_actions
        WHERE confirmation_id = %s
    """
    params = [confirmation_id]
    if user_id is not None:
        sql += " AND user_id = %s"
        params.append(user_id)
    if conversation_id is not None:
        sql += " AND conversation_id = %s"
        params.append(conversation_id)
    sql += " LIMIT 1"
    cursor.execute(sql, tuple(params))

    return cursor.fetchone()

def update_agent_pending_action_status(
    cursor,
    confirmation_id: str,
    new_status: str
):
    cursor.execute("""
        UPDATE agent_pending_actions
        SET status = %s
        WHERE confirmation_id = %s
          AND status = 'pending'
    """, (
        new_status,
        confirmation_id
    ))

    return cursor.rowcount

def expire_agent_pending_actions(cursor, max_age_days: int = 7):
    cursor.execute("""
        UPDATE agent_pending_actions
        SET status = 'expired'
        WHERE status = 'pending'
          AND created_at <= DATE_SUB(NOW(), INTERVAL %s DAY)
    """, (max_age_days,))
    return cursor.rowcount

def expire_agent_pending_action_if_due(
    cursor, confirmation_id: str, user_id: int, max_age_days: int = 7
):
    cursor.execute("""
        UPDATE agent_pending_actions
        SET status = 'expired'
        WHERE confirmation_id = %s
          AND user_id = %s
          AND status = 'pending'
          AND created_at <= DATE_SUB(NOW(), INTERVAL %s DAY)
    """, (confirmation_id, user_id, max_age_days))
    return cursor.rowcount

def clear_expired_current_confirmations(cursor, confirmation_id: str | None = None):
    sql = """
        UPDATE agent_sessions AS s
        JOIN agent_pending_actions AS a
          ON s.current_confirmation_id = a.confirmation_id
        SET s.current_confirmation_id = NULL,
            s.status = 'completed'
        WHERE a.status = 'expired'
    """
    params = ()
    if confirmation_id is not None:
        sql += " AND a.confirmation_id = %s"
        params = (confirmation_id,)
    cursor.execute(sql, params)
    return cursor.rowcount

def claim_agent_pending_action_execution(
    cursor,
    confirmation_id: str
):
    cursor.execute("""
        UPDATE agent_pending_actions
        SET status = 'executing'
        WHERE confirmation_id = %s
          AND status = 'confirmed'
    """, (
        confirmation_id,
    ))

    return cursor.rowcount

def confirm_agent_pending_action(
    cursor,
    confirmation_id: str,
    max_age_days: int = 7
):
    cursor.execute("""
        UPDATE agent_pending_actions
        SET
            status = 'confirmed',
            confirmed_at = NOW()
        WHERE confirmation_id = %s
          AND status = 'pending'
          AND created_at > DATE_SUB(NOW(), INTERVAL %s DAY)
    """, (
        confirmation_id,
        max_age_days,
    ))

    return cursor.rowcount

def finish_agent_pending_action(
    cursor,
    confirmation_id: str,
    status: str,
    execution_result_json: str,
    error_message: str | None = None,
):
    cursor.execute("""
        UPDATE agent_pending_actions
        SET
            status = %s,
            execution_result_json = %s,
            error_message = %s,
            executed_at = NOW()
        WHERE confirmation_id = %s
          AND status = 'executing'
    """, (
        status,
        execution_result_json,
        error_message,
        confirmation_id
    ))

    return cursor.rowcount


def finish_agent_task_for_action(cursor, confirmation_id: str, action_status: str):
    """在 Action 收尾事务内更新所属任务；同一任务以最新 Action 为准。"""
    task_status = "completed" if action_status == "executed" else "failed"
    cursor.execute("""
        UPDATE agent_tasks AS t
        JOIN agent_pending_actions AS a
          ON a.task_id = t.id AND a.user_id = t.user_id
        SET t.status = %s, t.current_step = %s,
            t.execution_id=NULL, t.execution_started_at=NULL,
            t.execution_heartbeat_at=NULL
        WHERE a.confirmation_id = %s
          AND a.status = %s
          AND (t.execution_id IS NULL OR
               t.execution_id=CONCAT('CONFEXEC_', MD5(a.confirmation_id)))
          AND t.status IN ('running','waiting_confirm','waiting_input')
          AND NOT EXISTS (
              SELECT 1 FROM agent_pending_actions AS newer
              WHERE newer.task_id = a.task_id AND newer.id > a.id
          )
    """, (task_status, task_status, confirmation_id, action_status))
    return cursor.rowcount

def get_after_sale_by_idempotency_key(
    cursor,
    idempotency_key: str
):
    cursor.execute("""
        SELECT *
        FROM after_sales
        WHERE idempotency_key = %s
        LIMIT 1
    """, (
        idempotency_key,
    ))

    return cursor.fetchone()
