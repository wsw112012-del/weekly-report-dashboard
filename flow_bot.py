"""flow_bot.py — Flow v1 Bot API 얇은 클라이언트.

엔드포인트 명세 (사용자 제공 + 공식 문서 캡처 확인):
  - 인증: x-flow-api-key 헤더
  - GET  /v1/bots                       : 발급된 봇 목록 조회
  - POST /v1/bots/{botId}/posts         : 게시글 작성

응답 envelope: {"response": {"success": true, "code": 200, "message": "...", "data": {...}}}
"""
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class FlowBot:
    BASE = "https://api.flow.team"

    def __init__(self, api_key: str, verify_ssl: bool = False):
        # 사내망 self-signed cert 인터셉트 대응 — 기본 verify=False
        self._h = {"Content-Type": "application/json", "x-flow-api-key": api_key}
        self._verify = verify_ssl

    def list_bots(self) -> list[dict]:
        r = requests.get(f"{self.BASE}/v1/bots",
                         headers=self._h, verify=self._verify, timeout=15)
        r.raise_for_status()
        return r.json().get("response", {}).get("data", {}).get("bots", [])

    def create_post(self, bot_id: str, project_id: str | int, title: str, contents: str,
                    files: list | None = None, image_files: list | None = None,
                    view_permission: str | None = None) -> dict:
        body: dict = {
            "projectId": str(project_id),
            "title": title,
            "contents": contents,
        }
        if files:           body["files"] = files
        if image_files:     body["imageFiles"] = image_files
        if view_permission: body["viewPermission"] = view_permission
        r = requests.post(f"{self.BASE}/v1/bots/{bot_id}/posts",
                          headers=self._h, json=body,
                          verify=self._verify, timeout=15)
        r.raise_for_status()
        return r.json()
