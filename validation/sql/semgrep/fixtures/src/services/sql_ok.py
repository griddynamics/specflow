"""Real-scan fixture for sql.yml's `kit.sql.commit-outside-service`, the `ok` half:
nested under src/services/ so `paths.exclude` shields it."""


class OrderService:
    def __init__(self, session: object) -> None:
        self._session = session

    async def submit(self, order_id: str) -> None:
        await self._session.execute(mark_submitted(order_id))
        # ok: kit.sql.commit-outside-service
        await self._session.commit()


def mark_submitted(order_id: str) -> object: ...
