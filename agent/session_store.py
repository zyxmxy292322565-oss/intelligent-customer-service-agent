from db.connection import get_connection



def save_current_confirmation(
    user_id: int,
    confirmation_id: str
):
    """
    保存用户当前等待确认的操作。

    一个用户只保留一个当前确认任务。
    新任务会覆盖旧任务。
    """

    connection = get_connection()

    try:
        cursor = connection.cursor()


        sql = """
        INSERT INTO agent_sessions
        (
            user_id,
            current_confirmation_id,
            status
        )
        VALUES
        (
            %s,
            %s,
            'active'
        )

        ON DUPLICATE KEY UPDATE

        current_confirmation_id = VALUES(
            current_confirmation_id
        ),

        status = 'active'
        """


        cursor.execute(
            sql,
            (
                user_id,
                confirmation_id
            )
        )


        connection.commit()


    except Exception:

        connection.rollback()

        raise


    finally:

        cursor.close()

        connection.close()



def get_current_confirmation(
    user_id:int
):

    connection = get_connection()

    cursor = None

    try:

        cursor = connection.cursor()

        sql = """
        SELECT current_confirmation_id
        FROM agent_sessions
        WHERE user_id=%s
        AND status='active'
        LIMIT 1
        """

        cursor.execute(
            sql,
            (
                user_id,
            )
        )

        row = cursor.fetchone()


        if (
            row is None
            or row["current_confirmation_id"] is None
        ):
            return None


        return row["current_confirmation_id"]


    finally:

        if cursor:
            cursor.close()

        connection.close()



def clear_current_confirmation(
    user_id:int,
    confirmation_id:str
):
    """
    操作完成后清除当前确认状态。
    """

    connection=get_connection()


    try:

        cursor=connection.cursor()


        sql="""
        UPDATE agent_sessions

        SET
            current_confirmation_id=NULL,

            status='completed'

        WHERE user_id=%s
          AND current_confirmation_id=%s
        """


        cursor.execute(
            sql,
            (
                user_id,
                confirmation_id,
            )
        )


        connection.commit()


    except Exception:

        connection.rollback()

        raise


    finally:

        cursor.close()

        connection.close()
