from services.order_service import (
    get_user_orders_service
)

from agent.state import AgentState
import logging
from core.logging import log_extra

logger = logging.getLogger("agent.context_resolver")


def resolve_order_item_by_product(
    user_id: int,
    product_name: str,
    trace_id: str | None = None
):
    """
    根据当前用户和商品名称，
    从真实订单数据中解析对应订单和商品。
    """

    logger.info("order resolution started user_id=%s", user_id,
                extra=log_extra(trace_id=trace_id))

    result = get_user_orders_service(
        user_id
    )

    if result is None:
        return {
            "success": False,
            "code": "USER_NOT_FOUND",
            "message": "用户不存在"
        }


    matched_items = []


    for order in result["orders"]:

        for item in order["items"]:

            item_product_name = item[
                "product_name"
            ]

            if product_name in item_product_name:

                matched_items.append({

                    "user_id": order.get("user_id"),

                    "order_id": order["id"],

                    "order_no": order["order_no"],

                    "order_status": order["status"],

                    "created_at": order.get("created_at"),

                    "paid_at": order.get("paid_at"),

                    "delivered_at": order.get("delivered_at"),

                    "order_item_id": item["id"],

                    "product_name": item_product_name,

                    "category": item.get("category"),

                    "quantity": item["quantity"],

                    "unit_price": item["unit_price"]

                })


    if not matched_items:

        return {

            "success": False,

            "code": "PRODUCT_NOT_FOUND",

            "message": "没有找到对应商品订单"

        }



    if len(matched_items) > 1:

        return {

            "success": False,

            "code": "MULTIPLE_PRODUCTS_FOUND",

            "message": "找到多个符合条件的商品订单",

            "data": {

                "candidates": matched_items

            }

        }



    logger.info("order resolution succeeded", extra=log_extra(trace_id=trace_id))


    return {

        "success": True,

        "code": "SUCCESS",

        "message": "订单商品解析成功",

        "data": matched_items[0]

    }



def resolve_state_context(
    state: AgentState
) -> dict:

    params = state["params"]

    product_name = params.get(
        "product_name"
    )


    if not product_name:

        return {

            "error": {

                "code": "PRODUCT_NAME_REQUIRED",

                "message": "缺少商品名称，无法解析订单上下文"

            }

        }



    result = resolve_order_item_by_product(

        user_id=state["current_user_id"],

        product_name=product_name,

        trace_id=state.get("trace_id")

    )



    if not result["success"]:


        error_code = result["code"]

        result_data = result.get(
            "data"
        )


        if error_code == "MULTIPLE_PRODUCTS_FOUND":


            candidates = []


            if isinstance(
                result_data,
                list
            ):

                candidates = result_data


            elif isinstance(
                result_data,
                dict
            ):

                candidates = result_data.get(
                    "candidates",
                    []
                )


            return {

                "resolved_context": {

                    "candidates": candidates

                },

                "error": {

                    "code": error_code,

                    "message": result["message"]

                }

            }



        return {

            "error": {

                "code": error_code,

                "message": result["message"],

                "data": result_data

            }

        }




    data = result["data"]


    return {

        "resolved_context": {

            "user_id": data["user_id"],


            "order_id": data["order_id"],


            "order_no": data["order_no"],


            "order_status": data["order_status"],

            "created_at": data["created_at"],

            "paid_at": data["paid_at"],

            "delivered_at": data["delivered_at"],


            "order_item_id": data["order_item_id"],


            "product_name": data["product_name"],

            "category": data["category"],


            "quantity": data["quantity"],


            "unit_price": data["unit_price"]

        },


        "error": None

    }





def select_context_candidate(
    state: AgentState,
    selected_order_no: str
) -> dict:


    candidates = (

        state["resolved_context"]

        .get(

            "candidates",

            []

        )

    )


    if not candidates:


        return {

            "error": {

                "code": "CANDIDATE_LIST_NOT_FOUND",

                "message": "当前没有可供选择的候选订单"

            }

        }



    matched_candidates = [

        candidate

        for candidate in candidates

        if candidate.get(
            "order_no"
        )
        == selected_order_no

    ]



    if len(matched_candidates) == 0:


        return {

            "error": {

                "code": "INVALID_CANDIDATE_SELECTION",

                "message": "选择的订单不在候选列表中",

                "data": {

                    "selected_order_no":
                        selected_order_no

                }

            }

        }



    if len(matched_candidates) > 1:


        return {

            "error": {

                "code": "CANDIDATE_SELECTION_AMBIGUOUS",

                "message": (
                    "该订单仍然对应多个候选商品，"
                    "需要进一步确认"
                )

            }

        }



    selected_candidate = dict(
        matched_candidates[0]
    )


    return {

        "resolved_context":
            selected_candidate,


        "error": None

    }
