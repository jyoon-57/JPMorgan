"""
JPMorgan AI Trading Bot - main_bot.py
1시간마다 한국 주식시장을 분석하고 텔레그램으로 알림을 보내는 자율 매매 봇.
Pipeline: Market Analyst → Quant Strategist → Risk Officer → Telegram
"""

import json
import os
import re
import time
from datetime import datetime, timedelta
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

KST = ZoneInfo("Asia/Seoul")
GEMINI_MODEL = "gemini-2.5-flash"

# Gemini Client (모듈 로드 시 초기화하지 않고 main()에서 생성)
gemini_client: genai.Client = None


# ============================================================
# 🛠 유틸리티 함수
# ============================================================
def load_skill_prompt(agent_name: str) -> str:
    """SKILL.md에서 YAML Frontmatter를 제거하고 System Prompt(Markdown 본문)만 추출."""
    skill_path = SKILLS_DIR / agent_name / "SKILL.md"

    if not skill_path.exists():
        raise FileNotFoundError(f"에이전트 설정 파일을 찾을 수 없습니다: {skill_path}")

    raw = skill_path.read_text(encoding="utf-8")

    # --- 로 감싸진 YAML Frontmatter 제거
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
        print(f"[💾] 주문 내역 저장 완료 → {ORDERS_FILE}")
    except json.JSONDecodeError:
        print(f"[⚠️] 유효하지 않은 JSON이라 저장하지 않습니다: {orders_json[:50]}...")


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
            print("[📨] 텔레그램 전송 성공")
        else:
            print(f"[⚠️] 텔레그램 전송 실패: {resp.status_code} {resp.text}")
    except requests.RequestException as e:
        print(f"[⚠️] 텔레그램 연결 오류: {e}")


# ============================================================
# 📡 실시간 시장 데이터 수집 (pykrx + FinanceDataReader)
# ============================================================
def fetch_market_data() -> str:
    """pykrx와 FinanceDataReader로 최신 시장 데이터를 수집하여 텍스트로 반환."""
    from pykrx import stock
    import FinanceDataReader as fdr

    now = datetime.now(KST)
    today_str = now.strftime("%Y%m%d")
    # pykrx는 장중에 당일 데이터가 불완전할 수 있으므로 최근 5영업일 범위로 조회
    start_str = (now - timedelta(days=7)).strftime("%Y%m%d")

    sections = []

    # ── 1) KOSPI / KOSDAQ 지수 ──
    try:
        kospi = stock.get_index_ohlcv(start_str, today_str, "1001")  # KOSPI
        kosdaq = stock.get_index_ohlcv(start_str, today_str, "2001")  # KOSDAQ

        if not kospi.empty:
            latest_kospi = kospi.iloc[-1]
            prev_kospi = kospi.iloc[-2] if len(kospi) >= 2 else latest_kospi
            kospi_change = ((latest_kospi["종가"] - prev_kospi["종가"]) / prev_kospi["종가"]) * 100
            sections.append(
                f"KOSPI: {latest_kospi['종가']:,.2f} "
                f"(전일 대비 {kospi_change:+.2f}%) "
                f"[시가 {latest_kospi['시가']:,.2f} / 고가 {latest_kospi['고가']:,.2f} / 저가 {latest_kospi['저가']:,.2f}]"
            )

        if not kosdaq.empty:
            latest_kosdaq = kosdaq.iloc[-1]
            prev_kosdaq = kosdaq.iloc[-2] if len(kosdaq) >= 2 else latest_kosdaq
            kosdaq_change = ((latest_kosdaq["종가"] - prev_kosdaq["종가"]) / prev_kosdaq["종가"]) * 100
            sections.append(
                f"KOSDAQ: {latest_kosdaq['종가']:,.2f} "
                f"(전일 대비 {kosdaq_change:+.2f}%)"
            )
    except Exception as e:
        sections.append(f"[지수 데이터 조회 실패: {e}]")

    time.sleep(1)  # pykrx 요청 간 딜레이

    # ── 2) USD/KRW 환율 ──
    try:
        fdr_start = (now - timedelta(days=7)).strftime("%Y-%m-%d")
        fdr_today = now.strftime("%Y-%m-%d")
        usdkrw = fdr.DataReader("USD/KRW", fdr_start, fdr_today)
        if not usdkrw.empty:
            latest_rate = usdkrw.iloc[-1]["Close"]
            prev_rate = usdkrw.iloc[-2]["Close"] if len(usdkrw) >= 2 else latest_rate
            rate_change = ((latest_rate - prev_rate) / prev_rate) * 100
            sections.append(f"USD/KRW 환율: {latest_rate:,.2f}원 (전일 대비 {rate_change:+.2f}%)")
    except Exception as e:
        sections.append(f"[환율 데이터 조회 실패: {e}]")

    time.sleep(1)

    # ── 3) 외국인 순매수 상위 (KOSPI) ──
    try:
        # pykrx의 날짜 형식: YYYYMMDD
        # 장중이면 전일 데이터, 장 마감 후면 당일 데이터가 조회됨
        foreign_buy = stock.get_market_net_purchases_of_equities(
            start_str, today_str, "KOSPI", "외국인"
        )
        if not foreign_buy.empty:
            top5 = foreign_buy.head(5)
            lines = []
            for name, row in top5.iterrows():
                lines.append(f"  - {name}: {row['순매수거래량']:+,}주 / {row['순매수거래대금']:+,}원")
            sections.append("외국인 순매수 TOP 5 (KOSPI):\n" + "\n".join(lines))
    except Exception as e:
        sections.append(f"[외국인 수급 데이터 조회 실패: {e}]")

    time.sleep(1)

    # ── 4) 기관 순매수 상위 (KOSPI) ──
    try:
        inst_buy = stock.get_market_net_purchases_of_equities(
            start_str, today_str, "KOSPI", "기관합계"
        )
        if not inst_buy.empty:
            top5 = inst_buy.head(5)
            lines = []
            for name, row in top5.iterrows():
                lines.append(f"  - {name}: {row['순매수거래량']:+,}주 / {row['순매수거래대금']:+,}원")
            sections.append("기관 순매수 TOP 5 (KOSPI):\n" + "\n".join(lines))
    except Exception as e:
        sections.append(f"[기관 수급 데이터 조회 실패: {e}]")

    result = "\n".join(sections)
    print(f"[📡] 시장 데이터 수집 완료 ({len(sections)}개 항목)")
    return result


# ============================================================
# 🤖 Gemini API 호출
# ============================================================
def call_gemini(system_prompt: str, user_prompt: str) -> str:
    """일반 Gemini API 호출 (Quant, Risk Officer용)."""
    full_prompt = f"{system_prompt}\n\n---\n\n{user_prompt}"

    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=full_prompt,
    )

    if response.text:
        return response.text
    return "응답을 생성하지 못했습니다."


def call_gemini_with_search(system_prompt: str, user_prompt: str) -> str:
    """Google Search Grounding이 활성화된 Gemini API 호출 (Analyst용)."""
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
# 🔄 메인 파이프라인
# ============================================================
def run_pipeline() -> None:
    """Analyst → Quant → Risk Officer → Telegram 파이프라인 실행."""
    now_kst = datetime.now(KST)
    current_time = now_kst.strftime("%H:%M")
    current_datetime = now_kst.strftime("%Y-%m-%d %H:%M")

    print(f"\n{'='*60}")
    print(f"[🚀] 파이프라인 시작 — {current_datetime} KST")
    print(f"{'='*60}")

    try:
        # ── Step 0: 실시간 시장 데이터 수집 ──
        print("\n[0/4] 📡 시장 데이터 수집 중 (pykrx + FDR)...")
        market_data = fetch_market_data()

        # ── Step 1: Market Analyst (Google Search Grounding 활성화) ──
        print("\n[1/4] 📊 Market Analyst 호출 중 (웹 검색 활성화)...")
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
        print(f"[✓] Market Analysis 완료")

        # ── Step 2: Quant Strategist ──
        print("\n[2/4] 🧮 Quant Strategist 호출 중...")
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
        print(f"[✓] Quant Strategy 완료")

        # ── Step 3: Risk Officer ──
        print("\n[3/4] 🛡️ Risk Officer 호출 중...")
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
        print(f"[✓] Risk Assessment 완료")

        # ── Step 4: 텔레그램 전송 & 주문 저장 ──
        print("\n[4/4] 📨 결과 전송 및 저장...")
        send_telegram(final_message)
        save_orders(proposed_orders)

        print(f"\n[✅] 파이프라인 성공적으로 완료 — {current_datetime} KST")

    except Exception as e:
        error_msg = f"🔥 파이프라인 실행 중 오류 발생:\n{str(e)}"
        print(error_msg)
        send_telegram(f"⚠️ [ERROR] 봇 실행 중 오류 발생!\n{str(e)}")


# ============================================================
# ⏰ 스케줄러 설정
# ============================================================
def job():
    """스케줄러에 의해 실행되는 작업 함수."""
    now = datetime.now(KST)

    # 주말(토=5, 일=6) 체크
    if now.weekday() >= 5:
        print(f"[😴] 주말입니다. ({now.strftime('%A')}) 봇이 쉽니다.")
        return

    # 한국 정규장: 09:00 ~ 15:30
    start_time = now.replace(hour=9, minute=0, second=0, microsecond=0)
    end_time = now.replace(hour=15, minute=30, second=0, microsecond=0)

    if start_time <= now <= end_time:
        run_pipeline()
    else:
        print(f"[😴] 장 마감 시간입니다. ({now.strftime('%H:%M')})")


def main():
    """프로그램 진입점."""
    global gemini_client
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)

    print("=" * 60)
    print("  KRX Auto-Trading Bot v3.0 (Live Data + Search Grounding)")
    print(f"  Model: {GEMINI_MODEL}")
    print(f"  Target: KOSPI/KOSDAQ (09:00 ~ 15:30)")
    print("=" * 60)

    # 테스트를 위해 시작하자마자 1회 실행 (원치 않으면 주석 처리)
    # job()

    # 매 시간 정각에 실행 예약
    schedule.every().hour.at(":00").do(job)

    print("[⏰] 스케줄러 가동 중... (매 정각 실행)")

    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
