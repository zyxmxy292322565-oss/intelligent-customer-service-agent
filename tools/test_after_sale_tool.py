from tools.after_sale_tools import (
    get_user_after_sales_tool
)


def main():
    result = get_user_after_sales_tool.invoke({
        "user_id": 999999
    })

    print(result)


if __name__ == "__main__":
    main()
