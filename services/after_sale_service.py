import uuid
import pymysql

from services.policy_execution_service import (
    PolicyValidation, existing_after_sale_for_key,
    recheck_policy_under_lock, validate_policy_before_create,
)

from db.connection import get_connection
from db.repository import (
    get_user_by_id,
    get_order_by_order_no_and_user_id,
    get_order_item_by_id_and_order_id,
    get_active_after_sale_by_order_item_id,
    create_after_sale,
    create_after_sale_status_log,
    get_after_sale_by_id,
    update_after_sale_status,
    get_after_sales_by_user_id,
    get_after_sale_by_no_and_user_id,
    get_after_sale_status_logs_by_after_sale_id,
    get_after_sale_by_idempotency_key
)

ALLOWED_TRANSITIONS = {
    "pending": [
        "processing"
    ],
    "processing": [
        "approved",
        "rejected"
    ],
    "approved": [
        "completed"
    ],
    "rejected": [],
    "completed": []
}


def _create_replay(existing: dict, order_no: str) -> dict:
    return {
        "success": True, "code": "IDEMPOTENT_REPLAY",
        "message": "该写操作已经执行过，返回原售后结果",
        "data": {
            "id": existing["id"], "after_sale_no": existing["after_sale_no"],
            "user_id": existing["user_id"], "order_no": order_no,
            "order_item_id": existing["order_item_id"],
            "type": existing["type"], "status": existing["status"],
            "refund_amount": existing["refund_amount"], "idempotent_replay": True,
        },
    }

def create_after_sale_service(
    user_id: int,
    order_no: str,
    order_item_id: int,
    after_sale_type: str,
    reason: str,
    idempotency_key: str | None = None,
    _policy_validation: PolicyValidation | None = None,
    _policy_reason_text: str | None = None,
):
    """
    创建售后申请。

    返回：
    成功：返回售后申请信息
    失败：返回错误结果
    """

    # A committed operation is replayed before policy retrieval. This is not
    # a new application and must survive a later policy-service outage.
    if idempotency_key is not None:
        try:
            existing = existing_after_sale_for_key(idempotency_key, user_id)
        except pymysql.err.OperationalError:
            return {"success": False, "code": "DATABASE_RETRYABLE",
                    "message": "订单服务暂时不可用，请稍后重试。"}
        if existing is not None:
            return _create_replay(existing, order_no)

    if _policy_validation is None:
        try:
            gate = validate_policy_before_create(
                user_id=user_id, order_no=order_no, order_item_id=order_item_id,
                after_sale_type=after_sale_type, reason=reason,
            )
        except pymysql.err.OperationalError:
            return {"success": False, "code": "DATABASE_RETRYABLE",
                    "message": "订单服务暂时不可用，请稍后重试。"}
        if not gate["success"]:
            return gate
        _policy_validation = gate["validation"]
    elif not isinstance(_policy_validation, PolicyValidation):
        return {"success": False, "code": "POLICY_VALIDATION_REQUIRED",
                "message": "需要重新进行政策核验。"}

    try:
        connection = get_connection()
    except pymysql.err.OperationalError:
        return {"success": False, "code": "DATABASE_RETRYABLE",
                "message": "订单服务暂时不可用，请稍后重试。"}
    cursor = connection.cursor()

    try:
        # READ COMMITTED ensures a waiting contender sees the first commit
        # after acquiring the item lock, even if it did an idempotency read.
        cursor.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
        # =====================================================
        # 0. Idempotency Check（幂等检查）
        # =====================================================

        if idempotency_key is not None:

            existing_after_sale = (
                get_after_sale_by_idempotency_key(
                    cursor=cursor,
                    idempotency_key=idempotency_key
                )
            )

            if existing_after_sale is not None:
                if existing_after_sale["user_id"] != user_id:
                    return {"success": False, "code": "ORDER_NOT_FOUND", "message": "订单不存在"}
                return _create_replay(existing_after_sale, order_no)

        # =====================================================
        # 1. 查询订单，并确认订单属于当前用户
        # =====================================================

        order = get_order_by_order_no_and_user_id(
            cursor,
            user_id,
            order_no,
            lock_for_share=True,
        )

        if order is None:
            return {
                "success": False,
                "code": "ORDER_NOT_FOUND",
                "message": "订单不存在"
            }

        # 2. 查询订单商品，并确认商品属于当前订单
        order_item = get_order_item_by_id_and_order_id(
            cursor,
            order_item_id,
            order["id"],
            for_update=True,
        )

        if order_item is None:
            return {
                "success": False,
                "code": "ORDER_ITEM_NOT_FOUND",
                "message": "订单商品不存在"
            }

        policy_in_transaction = recheck_policy_under_lock(
            _policy_validation, user_id=user_id, order_no=order_no,
            order=order, item=order_item, after_sale_type=after_sale_type,
            reason=_policy_reason_text or reason,
        )
        if not policy_in_transaction["success"]:
            return policy_in_transaction

        # 3. 检查是否已经有进行中的售后
        active_after_sale = (
            get_active_after_sale_by_order_item_id(
                cursor,
                order_item_id
            )
        )

        if active_after_sale is not None:
            return {
                "success": False,
                "code": "AFTER_SALE_ALREADY_EXISTS",
                "message": "该商品已经存在进行中的售后申请"
            }

        # 4. 根据业务规则检查订单状态
        allowed_statuses = [
            "paid",
            "shipped",
            "delivered"
        ]

        if order["status"] not in allowed_statuses:
            return {
                "success": False,
                "code": "ORDER_STATUS_NOT_ALLOWED",
                "message": "当前订单状态不允许申请售后"
            }

        # 5. 生成售后业务编号
        after_sale_no = (
            "AS"
            + uuid.uuid4().hex[:16].upper()
        )

        # 6. 计算退款金额
        refund_amount = None

        if after_sale_type in [
            "refund",
            "return_refund"
        ]:
            refund_amount = (
                order_item["unit_price"]
                * order_item["quantity"]
            )

        # 7. 创建售后记录
        after_sale_id = create_after_sale(
            cursor=cursor,
            after_sale_no=after_sale_no,
            user_id=user_id,
            order_id=order["id"],
            order_item_id=order_item_id,
            after_sale_type=after_sale_type,
            reason=reason,
            status="pending",
            refund_amount=refund_amount,
            idempotency_key=idempotency_key
        )

        create_after_sale_status_log(
            cursor=cursor,
            after_sale_id=after_sale_id,
            from_status=None,
            to_status="pending",
            operator_type="user",
            operator_id=str(user_id),
            remark="用户创建售后申请"
        )

        # 8. 提交事务
        connection.commit()

        return {
            "success": True,
            "code": "SUCCESS",
            "message": "售后申请创建成功",
            "data": {
                "id": after_sale_id,
                "after_sale_no": after_sale_no,
                "user_id": user_id,
                "order_no": order_no,
                "order_item_id": order_item_id,
                "type": after_sale_type,
                "status": "pending",
                "refund_amount": refund_amount
            }
        }

    except (pymysql.err.OperationalError,) as exc:
        connection.rollback()
        if exc.args and exc.args[0] in {1205, 1213}:
            return {"success": False, "code": "DATABASE_RETRYABLE",
                    "message": "订单处理繁忙，请稍后重试。"}
        raise
    except Exception:
        connection.rollback()
        raise

    finally:
        cursor.close()
        connection.close()

def update_after_sale_status_service(
    after_sale_id: int,
    new_status: str
):
    """
    更新售后状态。
    """

    connection = get_connection()
    cursor = connection.cursor()

    try:
        # 1. 查询售后记录
        after_sale = get_after_sale_by_id(
            cursor,
            after_sale_id
        )

        if after_sale is None:
            return {
                "success": False,
                "code": "AFTER_SALE_NOT_FOUND",
                "message": "售后申请不存在"
            }

        # 2. 获取当前状态
        current_status = after_sale["status"]

        # 3. 获取当前状态允许跳转到哪些状态
        allowed_next_statuses = (
            ALLOWED_TRANSITIONS.get(
                current_status,
                []
            )
        )

        # 4. 检查目标状态是否合法
        if new_status not in allowed_next_statuses:
            return {
                "success": False,
                "code": "INVALID_STATUS_TRANSITION",
                "message": (
                    f"售后状态不允许从 "
                    f"{current_status} "
                    f"变更为 {new_status}"
                )
            }

        # 5. 更新状态
        affected_rows = update_after_sale_status(
            cursor,
            after_sale_id,
            new_status
        )

        if affected_rows != 1:
            connection.rollback()

            return {
                "success": False,
                "code": "STATUS_UPDATE_FAILED",
                "message": "售后状态更新失败"
            }

        create_after_sale_status_log(
            cursor=cursor,
            after_sale_id=after_sale_id,
            from_status=current_status,
            to_status=new_status,
            operator_type="system",
            operator_id=None,
            remark="售后状态更新"
        )

        # 6. 提交事务
        connection.commit()

        return {
            "success": True,
            "code": "SUCCESS",
            "message": "售后状态更新成功",
            "data": {
                "after_sale_id": after_sale_id,
                "old_status": current_status,
                "new_status": new_status
            }
        }

    except Exception:
        connection.rollback()
        raise

    finally:
        cursor.close()
        connection.close()

def get_user_after_sales_service(
    user_id: int
):
    """
    查询用户全部售后申请。
    """

    connection = get_connection()
    cursor = connection.cursor()

    try:
        user = get_user_by_id(
            cursor,
            user_id
        )

        if user is None:
            return {
                "success": False,
                "code": "USER_NOT_FOUND",
                "message": "用户不存在"
            }

        after_sales = get_after_sales_by_user_id(
            cursor,
            user_id
        )

        return {
            "success": True,
            "code": "SUCCESS",
            "message": "售后列表查询成功",
            "data": {
                "user": user,
                "after_sale_count": len(after_sales),
                "after_sales": after_sales
            }
        }

    finally:
        cursor.close()
        connection.close()

def get_after_sale_detail_service(
    user_id: int,
    after_sale_no: str
):
    """
    查询用户的具体售后详情。
    """

    connection = get_connection()
    cursor = connection.cursor()

    try:
        user = get_user_by_id(
            cursor,
            user_id
        )

        if user is None:
            return {
                "success": False,
                "code": "USER_NOT_FOUND",
                "message": "用户不存在"
            }

        after_sale = get_after_sale_by_no_and_user_id(
            cursor,
            user_id,
            after_sale_no
        )

        if after_sale is None:
            return {
                "success": False,
                "code": "AFTER_SALE_NOT_FOUND",
                "message": "售后申请不存在"
            }

        return {
            "success": True,
            "code": "SUCCESS",
            "message": "售后详情查询成功",
            "data": after_sale
        }

    finally:
        cursor.close()
        connection.close()

def get_after_sale_history_service(
    user_id: int,
    after_sale_no: str
):
    """
    查询用户某一条售后的完整状态历史。
    """

    connection = get_connection()
    cursor = connection.cursor()

    try:
        # 1. 查询用户
        user = get_user_by_id(
            cursor,
            user_id
        )

        if user is None:
            return {
                "success": False,
                "code": "USER_NOT_FOUND",
                "message": "用户不存在"
            }

        # 2. 查询售后，并进行数据归属检查
        after_sale = get_after_sale_by_no_and_user_id(
            cursor,
            user_id,
            after_sale_no
        )

        if after_sale is None:
            return {
                "success": False,
                "code": "AFTER_SALE_NOT_FOUND",
                "message": "售后申请不存在"
            }

        # 3. 根据售后内部ID查询完整状态历史
        status_logs = (
            get_after_sale_status_logs_by_after_sale_id(
                cursor,
                after_sale["id"]
            )
        )

        # 4. 返回完整结果
        return {
            "success": True,
            "code": "SUCCESS",
            "message": "售后状态历史查询成功",
            "data": {
                "after_sale_no": after_sale["after_sale_no"],
                "order_no": after_sale["order_no"],
                "product_name": after_sale["product_name"],
                "current_status": after_sale["status"],
                "history_count": len(status_logs),
                "history": status_logs
            }
        }

    finally:
        cursor.close()
        connection.close()
