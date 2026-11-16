"""Run the same JSON serializer used by the API; never deserialize pickle jobs."""

from redis import Redis
from rq import Queue, Worker
from rq.serializers import JSONSerializer

from app.main import REDIS_URL


def main():
    connection = Redis.from_url(REDIS_URL)
    queues = [
        Queue(f"queueops-{priority}", connection=connection, serializer=JSONSerializer)
        for priority in ("high", "normal", "low")
    ]
    Worker(queues, connection=connection, serializer=JSONSerializer).work()


if __name__ == "__main__":
    main()
