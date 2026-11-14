import json
import os
import time
import psycopg2
import redis as redis_client


REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
DB_DSN = os.environ.get("DB_DSN", "host=db user=postgres password=postgres")


def open_db_connection():
    while True:
        try:
            conn = psycopg2.connect(DB_DSN)
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS votes (
                        id   VARCHAR(255) NOT NULL UNIQUE,
                        vote VARCHAR(255) NOT NULL
                    )
                """)
            print("Connected to db", flush=True)
            return conn
        except psycopg2.OperationalError:
            print("Waiting for db", flush=True)
            time.sleep(1)


def open_redis_connection():
    while True:
        try:
            r = redis_client.Redis(host=REDIS_HOST, socket_connect_timeout=2)
            r.ping()
            print(f"Connected to redis at {REDIS_HOST}", flush=True)
            return r
        except (redis_client.exceptions.ConnectionError, redis_client.exceptions.TimeoutError):
            print("Waiting for redis", flush=True)
            time.sleep(1)


def update_vote(conn, voter_id, vote):
    with conn.cursor() as cur:
        try:
            cur.execute(
                "INSERT INTO votes (id, vote) VALUES (%s, %s)",
                (voter_id, vote),
            )
        except psycopg2.errors.UniqueViolation:
            conn.rollback()
            cur.execute(
                "UPDATE votes SET vote = %s WHERE id = %s",
                (vote, voter_id),
            )


def main():
    db = open_db_connection()
    r = open_redis_connection()

    while True:
        time.sleep(0.1)

        # Reconnect Redis if down
        try:
            r.ping()
        except (redis_client.exceptions.ConnectionError, redis_client.exceptions.TimeoutError):
            print("Reconnecting Redis", flush=True)
            r = open_redis_connection()

        raw = r.lpop("votes")
        if raw:
            data = json.loads(raw)
            print(f"Processing vote for '{data['vote']}' by '{data['voter_id']}'", flush=True)

            # Reconnect DB if down
            if db.closed:
                print("Reconnecting DB", flush=True)
                db = open_db_connection()
            else:
                update_vote(db, data["voter_id"], data["vote"])
        else:
            # Keepalive query
            with db.cursor() as cur:
                cur.execute("SELECT 1")


if __name__ == "__main__":
    main()
