"""
JPMorgan AI Trading Bot - main_bot.py
1시간마다 한국 주식시장을 분석하고 텔레그램으로 알림을 보내는 자율 매매 봇.
Pipeline: Market Analyst → Quant Strategist → Risk Officer → Telegram
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, date
from pathlib import Path
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

from google import genai
from google.genai import types
import requests
import schedule
from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# ============================================================
# 🔑 API Keys — .env 파일에서 로드
# ============================================================
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# ============================================================
# 📁 경로 설정
# ============================================================
BASE_DIR = Path(__file__).resolve().parent.parent
SKILLS_DIR = BASE_DIR / ".agent" / "skills"
ORDERS_FILE = BASE_DIR / "last_hour_orders.json"
REPORTS_DIR = BASE_DIR / "reports"
LOGS_DIR = BASE_DIR / "logs"
GLOBAL_STATE_FILE = BASE_DIR / "context" / "global_state.md"

KST = ZoneInfo("Asia/Seoul")
GEMINI_MODEL = "gemini-2.5-flash"

# Gemini Client (main()에서 초기화)
gemini_client: genai.Client = None

# ============================================================
# 📋 한국 공휴일 (2026년)
# ============================================================
KR_HOLIDAYS_2026 = {
    date(2026, 1, 1),   # 신정
    date(2026, 2, 16),  # 설날 연휴
    date(2026, 2, 17),  # 설날
    date(2026, 2, 18),  # 설날 연휴
    date(2026, 3, 1),   # 삼일절
    date(2026, 5, 5),   # 어린이날
    date(2026, 5, 24),  # 부처님오신날
    date(2026, 6, 6),   # 현충일
    date(2026, 8, 15),  # 광복절
    date(2026, 9, 24),  # 추석 연휴
    date(2026, 9, 25),  # 추석
    date(2026, 9, 26),  # 추석 연휴
    date(2026, 10, 3),  # 개천절
    date(2026, 10, 9),  # 한글날
    date(2026, 12, 25), # 크리스마스
}


# ============================================================
# 📝 로깅 설정
# ============================================================
def setup_logging() -> logging.Logger:
    """콘솔 + 파일 동시 로깅 설정."""
    LOGS_DIR.mkdir(exist_ok=True)

    logger = logging.getLogger("jpmorgan")
    logger.setLevel(logging.DEBUG)

    # 포맷
    fmt = logging.Formatter("[%(asctime)s] %(levelname)s — %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    # 콘솔 핸들러
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)
    logger.addHandler(console)

    # 파일 핸들러 (일별 로그)
    today_str = datetime.now(KST).strftime("%Y-%m-%d")
    file_handler = logging.FileHandler(LOGS_DIR / f"bot_{today_str}.log", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    return logger


log = setup_logging()


# ============================================================
# 🛠 유틸리티 함수
# ============================================================
def load_skill_prompt(agent_name: str) -> str:
    """SKILL.md에서 YAML Frontmatter를 제거하고 System Prompt(Markdown 본문)만 추출."""
    skill_path = SKILLS_DIR / agent_name / "SKILL.md"

    if not skill_path.exists():
        raise FileNotFoundError(f"에이전트 설정 파일을 찾을 수 없습니다: {skill_path}")

    raw = skill_path.read_text(encoding="utf-8")

    parts = raw.split("---", 2)
    if len(parts) >= 3:
        return parts[2].strip()
    return raw.strip()


def load_previous_orders() -> str:
    """last_hour_orders.json을 읽어 문자열로 반환. 없으면 빈 리스트."""
    if ORDERS_FILE.exists():
        return ORDERS_FILE.read_text(encoding="utf-8")
    return "[]"


def save_orders(orders_json: str) -> None:
    """새 주문 JSON을 last_hour_orders.json에 덮어쓰기."""
    try:
        json.loads(orders_json)
        ORDERS_FILE.write_text(orders_json, encoding="utf-8")
        log.info("주문 내역 저장 완료 → %s", ORDERS_FILE)
    except json.JSONDecodeError:
        log.warning("유효하지 않은 JSON이라 저장하지 않습니다: %s...", orders_json[:50])


def parse_json_from_response(text: str) -> str:
    """Gemini 응답에서 ```json ... ``` 코드 블록만 깔끔하게 추출."""
    match = re.search(r"```json\s*([\s\S]*?)```", text)
    if match:
        return match.group(1).strip()
    match = re.search(r"(\[[\s\S]*\])", text)
    if match:
        return match.group(1).strip()
    return text.strip()


def send_telegram(message: str) -> None:
    """텔레그램 Bot API로 메시지 전송."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.ok:
            log.info("텔레그램 전송 성공")
        else:
            log.error("텔레그램 전송 실패: %s %s", resp.status_code, resp.text)
    except requests.RequestException as e:
        log.error("텔레그램 연결 오류: %s", e)


# ============================================================
# 📡 실시간 시장 데이터 수집 (pykrx + FinanceDataReader)
# ============================================================
# ============================================================
# 📡 실시간 시장 데이터 수집 (KIS OpenAPI)
# ============================================================
def fetch_market_data() -> str:
    """KIS OpenAPI를 통해 실시간/장중 지수, 환율, 수급 데이터를 수집하여 포맷팅된 JSON 문자열 반환."""
    from src.data.kis_collector import KisAuth, KisData
    import json

    # KIS 연결 초기화
    try:
        auth = KisAuth()
        # 토큰 발급 (실패 시 로그 남기고 빈 데이터 반환 가능성 있음)
        # auth.auth() # get_token에서 자동 호출됨
        collector = KisData(auth)
    except Exception as e:
        log.error("KIS API 초기화 실패: %s", e)
        return json.dumps({"error": str(e)}, ensure_ascii=False)

    data = {
        "indices": {},
        "investors": {},
        "exchange_rate": None,
        "timestamp": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    }

    # ── 1) KOSPI / KOSDAQ 지수 ──
    for name, code in [("KOSPI", "0001"), ("KOSDAQ", "1001")]:
        try:
            res = collector.get_market_index(code)
            if res and res.get('rt_cd') == '0':
                # API 응답 구조에 따라 파싱 (output1이 차트/현재가 정보 포함 가정)
                # inquire-daily-index-chartprice 기준 output1의 첫번째 값 사용 등 점검 필요
                # 단순화하여 raw data 일부를 전달하거나 파싱. 
                # 여기서는 output1 (현재가 정보) 파싱 시도.
                val = res.get('output1')
                # 만약 리스트라면 첫번째 요소
                if isinstance(val, list) and val:
                    val = val[0]
                
                # 필요한 필드만 추출 (예시 키값 - 실제 응답 확인 후 조정 필요할 수 있음)
                # KIS API 문서 기준: stck_prpr(현재가), prdy_vrss(대비), prdy_ctrt(등락률) 등
                # inquire-daily-index-chartprice 응답키: bstp_nmiv_prpr(지수), bstp_nmiv_prdy_vrss(대비) 등
                # *실제 응답 키*는 API 문서 의존. 여기서는 가독성 위해 맵핑.
                data["indices"][name] = {
                    "price": val.get("bstp_nmiv_prpr") or val.get("stck_prpr"),
                    "change": val.get("bstp_nmiv_prdy_ctrt") or val.get("prdy_ctrt")
                }
            else:
                data["indices"][name] = {"error": res.get("msg1") if res else "Unknown error"}
        except Exception as e:
            log.error(f"{name} 지수 수집 실패: {e}")
            data["indices"][name] = {"error": str(e)}
            
        time.sleep(0.2) # API 제한 고려

    # ── 2) USD/KRW 환율 ──
    try:
        data["exchange_rate"] = collector.get_exchange_rate()
    except Exception as e:
        log.error(f"환율 수집 실패: {e}")

    # ── 3) 투자자별 매매동향 (KOSPI 기준) ──
    try:
        res = collector.get_investor_trend("0001") # KOSPI
        if res and res.get('rt_cd') == '0':
            # output 리스트 순회하며 개인/외국인/기관 찾기
            # KIS API 'inquire-investor' response structure check needed.
            # Assuming standard structure or raw dump for Analyst to interpret.
            # We will pass the raw output list for Analyst to parse 'Foreigner', 'Institution'
            data["investors"]["KOSPI"] = res.get("output", [])
        else:
            data["investors"]["KOSPI"] = {"error": res.get("msg1") if res else "Failed"}
    except Exception as e:
        log.error(f"수급 데이터 수집 실패: {e}")

    log.info("KIS 시장 데이터 수집 완료")
    
    # JSON 문자열로 변환하여 반환
    return json.dumps(data, ensure_ascii=False, indent=2)


# ============================================================
# 🤖 Gemini API 호출 (Retry 포함)
# ============================================================
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=8),
    retry=retry_if_exception_type(Exception),
    before_sleep=lambda retry_state: log.warning(
        "Gemini API 재시도 %d/3 (%s)",
        retry_state.attempt_number,
        retry_state.outcome.exception(),
    ),
)
def call_gemini(system_prompt: str, user_prompt: str) -> str:
    """일반 Gemini API 호출 (Quant, Risk Officer용). 실패 시 최대 3회 재시도."""
    full_prompt = f"{system_prompt}\n\n---\n\n{user_prompt}"

    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=full_prompt,
    )

    if response.text:
        return response.text
    return "응답을 생성하지 못했습니다."


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=8),
    retry=retry_if_exception_type(Exception),
    before_sleep=lambda retry_state: log.warning(
        "Gemini Search API 재시도 %d/3 (%s)",
        retry_state.attempt_number,
        retry_state.outcome.exception(),
    ),
)
def call_gemini_with_search(system_prompt: str, user_prompt: str) -> str:
    """Google Search Grounding이 활성화된 Gemini API 호출 (Analyst용). 실패 시 최대 3회 재시도."""
    full_prompt = f"{system_prompt}\n\n---\n\n{user_prompt}"

    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=full_prompt,
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
        ),
    )

    if response.text:
        return response.text
    return "응답을 생성하지 못했습니다."


# ============================================================
# 📄 리포트 자동 저장
# ============================================================
def save_report(current_datetime: str, market_analysis: str, proposed_orders: str, final_message: str) -> Path:
    """파이프라인 결과를 reports/YYYY-MM-DD_HH-MM.md로 저장."""
    REPORTS_DIR.mkdir(exist_ok=True)
    filename = current_datetime.replace(" ", "_").replace(":", "-") + ".md"
    report_path = REPORTS_DIR / filename

    content = (
        f"# Trading Report — {current_datetime} KST\n\n"
        f"## 1. Market Analysis\n{market_analysis}\n\n"
        f"## 2. Proposed Orders (JSON)\n```json\n{proposed_orders}\n```\n\n"
        f"## 3. Risk Assessment & Telegram Message\n{final_message}\n"
    )

    report_path.write_text(content, encoding="utf-8")
    log.info("리포트 저장 완료 → %s", report_path)
    return report_path


# ============================================================
# 🔄 global_state.md 자동 갱신
# ============================================================
def update_global_state(current_datetime: str, report_filename: str) -> None:
    """context/global_state.md의 Last Updated와 Recent Accomplishments를 갱신."""
    if not GLOBAL_STATE_FILE.exists():
        log.warning("global_state.md를 찾을 수 없습니다: %s", GLOBAL_STATE_FILE)
        return

    raw = GLOBAL_STATE_FILE.read_text(encoding="utf-8")

    # Last Updated 갱신
    raw = re.sub(
        r"(\*\*Date:\*\*) .+",
        f"\\1 {current_datetime}",
        raw,
    )
    raw = re.sub(
        r"(\*\*Last Actor:\*\*) .+",
        "\\1 Bot Pipeline (Analyst → Quant → Risk)",
        raw,
    )

    # Recent Accomplishments에 새 항목 추가 (중복 방지: 같은 시각 항목이 없을 때만)
    new_entry = f"- [x] **{current_datetime} Auto-Trading Report** → `reports/{report_filename}`"
    if new_entry not in raw:
        raw = raw.replace(
            "## 📝 Recent Accomplishments",
            f"## 📝 Recent Accomplishments\n{new_entry}",
        )

    GLOBAL_STATE_FILE.write_text(raw, encoding="utf-8")
    log.info("global_state.md 갱신 완료")


# ============================================================
# 🔄 메인 파이프라인
# ============================================================
def run_pipeline() -> None:
    """Analyst → Quant → Risk Officer → Telegram 파이프라인 실행."""
    now_kst = datetime.now(KST)
    current_time = now_kst.strftime("%H:%M")
    current_datetime = now_kst.strftime("%Y-%m-%d %H:%M")

    log.info("=" * 50)
    log.info("파이프라인 시작 — %s KST", current_datetime)
    log.info("=" * 50)

    try:
        # ── Step 0: 실시간 시장 데이터 수집 ──
        log.info("[0/4] 시장 데이터 수집 중 (pykrx + FDR)...")
        market_data = fetch_market_data()

        # ── Step 1: Market Analyst (Google Search Grounding) ──
        log.info("[1/4] Market Analyst 호출 중 (웹 검색 활성화)...")
        analyst_prompt = load_skill_prompt("market-analyst")
        analyst_user_prompt = (
            f"현재 한국 시간: {current_time}\n\n"
            f"## 실시간 시장 데이터 (자동 수집)\n{market_data}\n\n"
            f"위 데이터와 웹 검색 결과를 종합하여 오늘의 한국 주식시장 시황을 분석해주세요."
        )
        market_analysis = call_gemini_with_search(
            system_prompt=analyst_prompt,
            user_prompt=analyst_user_prompt,
        )
        log.info("[✓] Market Analysis 완료")

        # ── Step 2: Quant Strategist ──
        log.info("[2/4] Quant Strategist 호출 중...")
        quant_prompt = load_skill_prompt("quant-strategist")
        previous_orders = load_previous_orders()

        quant_user_prompt = (
            f"## Market Analysis (from Analyst)\n{market_analysis}\n\n"
            f"## Previous Orders (1시간 전)\n```json\n{previous_orders}\n```\n\n"
            f"위 분석과 이전 주문을 비교하여 새로운 매매 전략을 JSON으로 출력하세요."
        )

        proposed_orders_raw = call_gemini(
            system_prompt=quant_prompt,
            user_prompt=quant_user_prompt,
        )
        proposed_orders = parse_json_from_response(proposed_orders_raw)
        log.info("[✓] Quant Strategy 완료")

        # ── Step 3: Risk Officer ──
        log.info("[3/4] Risk Officer 호출 중...")
        risk_prompt = load_skill_prompt("risk-officer")

        risk_user_prompt = (
            f"## Proposed Orders (from Quant)\n```json\n{proposed_orders}\n```\n\n"
            f"기준 시간: {current_datetime}\n"
            f"위 주문서를 검수하고, CEO에게 보낼 텔레그램 알림 메시지를 작성하세요."
        )

        final_message = call_gemini(
            system_prompt=risk_prompt,
            user_prompt=risk_user_prompt,
        )
        log.info("[✓] Risk Assessment 완료")

        # ── Step 4: 텔레그램 전송 & 저장 ──
        log.info("[4/4] 결과 전송 및 저장...")
        send_telegram(final_message)
        save_orders(proposed_orders)

        # ── Step 5: 리포트 저장 & global_state 갱신 ──
        report_path = save_report(current_datetime, market_analysis, proposed_orders, final_message)
        update_global_state(current_datetime, report_path.name)

        log.info("파이프라인 성공적으로 완료 — %s KST", current_datetime)

    except Exception as e:
        log.error("파이프라인 실행 중 오류 발생: %s", e, exc_info=True)
        send_telegram(f"⚠️ [ERROR] 봇 실행 중 오류 발생!\n{e}")


# ============================================================
# ⏰ 스케줄러 설정
# ============================================================
def is_market_closed(now: datetime) -> str | None:
    """장이 닫혀 있으면 사유 문자열 반환, 열려 있으면 None."""
    # 주말
    if now.weekday() >= 5:
        return f"주말입니다. ({now.strftime('%A')})"

    # 공휴일
    if now.date() in KR_HOLIDAYS_2026:
        return "공휴일입니다. (한국 증시 휴장)"

    # 장 시간 외 (09:00 ~ 15:30)
    start = now.replace(hour=9, minute=0, second=0, microsecond=0)
    end = now.replace(hour=15, minute=30, second=0, microsecond=0)
    if not (start <= now <= end):
        return f"장 마감 시간입니다. ({now.strftime('%H:%M')})"

    return None


def job():
    """스케줄러에 의해 실행되는 작업 함수."""
    now = datetime.now(KST)
    reason = is_market_closed(now)
    if reason:
        log.info("스킵 — %s", reason)
        return
    run_pipeline()


def main():
    """프로그램 진입점."""
    global gemini_client
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)

    log.info("=" * 50)
    log.info("KRX Auto-Trading Bot v4.0")
    log.info("Model: %s", GEMINI_MODEL)
    log.info("Target: KOSPI/KOSDAQ (09:00 ~ 15:30)")
    log.info("=" * 50)

    # 테스트를 위해 시작하자마자 1회 실행 (원치 않으면 주석 처리)
    # run_pipeline()

    # 매 시간 정각에 실행 예약
    schedule.every().hour.at(":00").do(job)

    log.info("스케줄러 가동 중... (매 정각 실행)")

    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
