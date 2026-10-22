import threading

from agent.action_executor import (
    execute_confirmed_action
)


def run_execute():

    result = execute_confirmed_action(
        confirmation_id="CONF_TEST",
        current_user_id=1
    )

    print(result)



def main():
    threads = [threading.Thread(target=run_execute) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


if __name__ == "__main__":
    main()
