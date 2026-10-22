from tools.order_tools import get_order_detail_tool


def main():

    print("工具名称：")
    print(get_order_detail_tool.name)

    print("\n工具描述：")
    print(get_order_detail_tool.description)

    print("\n工具参数：")
    print(
        get_order_detail_tool.args_schema.model_json_schema()
    )

    print("\n开始执行工具：")

    result = get_order_detail_tool.invoke({
        "user_id": 1,
        "order_no": "ORD202609070003"
    })

    print(result)


if __name__ == "__main__":
    main()