from __future__ import annotations

import time
from datetime import datetime
from typing import Any

import requests


GATEWAY_URL = "https://i.weread.qq.com/api/agent/gateway"


class WeReadError(RuntimeError):
    pass


class WeReadClient:
    def __init__(self, api_key: str, skill_version: str = "1.0.4", session=None):
        self.skill_version = skill_version
        self.session = session or requests.Session()
        self.session.headers.update(
            {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        )

    def call(
        self,
        api_name: str,
        *,
        timeout: int = 45,
        retries: int = 3,
        **params: Any,
    ) -> dict[str, Any]:
        payload = {"api_name": api_name, "skill_version": self.skill_version, **params}
        last_error = None
        for attempt in range(retries):
            try:
                response = self.session.post(GATEWAY_URL, json=payload, timeout=timeout)
                response.raise_for_status()
                data = response.json()
                if data.get("upgrade_info"):
                    message = (
                        data["upgrade_info"].get("message") or data["upgrade_info"]
                    )
                    raise WeReadError(f"微信读书技能需要升级：{message}")
                if data.get("errcode", 0) != 0:
                    raise WeReadError(
                        f"{api_name} 返回 errcode={data.get('errcode')}: {data.get('errmsg', '')}"
                    )
                return data
            except WeReadError:
                raise
            except Exception as exc:  # requests and transient JSON failures
                last_error = exc
                if attempt < retries - 1:
                    time.sleep(2**attempt)
        raise WeReadError(f"{api_name} 请求失败：{last_error}")

    def shelf(self) -> dict[str, Any]:
        return self.call("/shelf/sync")

    def notebooks(self) -> tuple[list[dict[str, Any]], dict[str, int]]:
        rows: list[dict[str, Any]] = []
        last_sort = None
        totals = {"books": 0, "notes": 0}
        while True:
            params: dict[str, Any] = {"count": 100}
            if last_sort is not None:
                params["lastSort"] = last_sort
            data = self.call("/user/notebooks", **params)
            batch = data.get("books") or []
            rows.extend(batch)
            totals = {
                "books": int(data.get("totalBookCount") or len(rows)),
                "notes": int(data.get("totalNoteCount") or 0),
            }
            if not data.get("hasMore") or not batch:
                break
            last_sort = batch[-1].get("sort")
        return rows, totals

    def book_bundle(self, book_id: str) -> dict[str, Any]:
        def safe_call(api_name: str, **params: Any) -> dict[str, Any]:
            try:
                # Book-level endpoints should fail fast so one problematic title
                # cannot block the entire scheduled sync for many minutes.
                return self.call(
                    api_name,
                    timeout=12,
                    retries=2,
                    **params,
                )
            except WeReadError as exc:
                print(f"⚠️ 跳过书籍 {book_id} 的接口 {api_name}: {exc}")
                return {}

        info = safe_call("/book/info", bookId=book_id)
        progress = safe_call("/book/getprogress", bookId=book_id).get("book") or {}
        chapter_data = safe_call("/book/chapterinfo", bookId=book_id)
        bookmark_data = safe_call("/book/bookmarklist", bookId=book_id)
        reviews: list[dict[str, Any]] = []
        synckey = 0
        review_pages = 0
        while review_pages < 20:
            page = safe_call(
                "/review/list/mine", bookid=book_id, synckey=synckey, count=100
            )
            if not page:
                break
            reviews.extend(
                (item.get("review") or item) for item in (page.get("reviews") or [])
            )
            review_pages += 1
            if not page.get("hasMore"):
                break
            next_key = page.get("synckey")
            if next_key == synckey:
                break
            synckey = next_key
        if review_pages >= 20 and page.get("hasMore"):
            print(f"⚠️ 书籍 {book_id} 的笔记分页超过 20 页，已停止继续读取")
        return {
            "info": info,
            "progress": progress,
            "chapters": chapter_data.get("chapters") or [],
            "highlights": bookmark_data.get("updated") or [],
            "reviews": reviews,
        }

    def reading_days(
        self, start_year: int
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        overall = self.call("/readdata/detail", mode="overall")
        current_year = datetime.now().year
        days: dict[int, int] = {}
        annual: dict[int, dict[str, Any]] = {}
        for year in range(start_year, current_year + 1):
            base_time = int(datetime(year, 1, 15).timestamp())
            data = self.call("/readdata/detail", mode="annually", baseTime=base_time)
            annual[year] = data
            daily = data.get("dailyReadTimes") or {}
            # The current gateway commonly omits dailyReadTimes in annual mode.
            # annual.readTimes is monthly, so query only the non-empty months to
            # obtain their day buckets from monthly.readTimes.
            if not daily:
                for month_timestamp, month_seconds in (data.get("readTimes") or {}).items():
                    if not int(month_seconds or 0):
                        continue
                    monthly = self.call(
                        "/readdata/detail",
                        mode="monthly",
                        baseTime=int(month_timestamp),
                    )
                    month_days = monthly.get("dailyReadTimes") or monthly.get("readTimes") or {}
                    daily.update(month_days)
            for timestamp, seconds in daily.items():
                days[int(timestamp)] = int(seconds or 0)
        return [
            {"timestamp": timestamp, "duration": duration}
            for timestamp, duration in sorted(days.items())
            if duration > 0
        ], {"overall": overall, "annual": annual}
