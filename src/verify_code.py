import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

def verify_code_integrity(file_path_str, keyword):
    """
    파일의 물리적 상태와 내용을 검증하여 AI 할루시네이션을 판별합니다.
    """
    # 1. 파일 존재 여부 확인
    base_dir = Path(__file__).resolve().parent.parent
    full_path = base_dir / file_path_str
    
    print(f"\n🔍 [INTEGRITY CHECK] Target: {file_path_str}")
    
    if not full_path.exists():
        print(f"❌ [FAILED] 파일이 존재하지 않습니다: {full_path}")
        sys.exit(1)

    # 2. 최근 수정 시간 확인 (AI가 실제로 파일을 건드렸는지 증거 확보)
    # 현재 시간과 파일의 마지막 수정 시간(mtime) 비교
    file_mtime = datetime.fromtimestamp(full_path.stat().st_mtime)
    now = datetime.now()
    time_diff = now - file_mtime

    print(f"   - 마지막 수정 시간: {file_mtime.strftime('%H:%M:%S')}")
    print(f"   - 현재 시간: {now.strftime('%H:%M:%S')}")
    
    # 60초 이내에 수정되지 않았다면 작성하지 않은 것으로 간주
    if time_diff > timedelta(seconds=60):
        print(f"⚠️  [HALLUCINATION ALERT] 파일이 최근 60초 이내에 수정되지 않았습니다.")
        print(f"   (마지막 수정 후 {time_diff.seconds}초 경과. AI가 수정을 건너뛰었을 가능성 농후)")
        sys.exit(2)

    # 3. 핵심 키워드 존재 여부 확인
    content = full_path.read_text(encoding='utf-8')
    if keyword in content:
        print(f"✅ [VERIFIED] '{keyword}' 내용이 파일에 정상 반영되었습니다.")
        sys.exit(0) # 성공
    else:
        print(f"❌ [FAILED] 파일은 수정되었으나, 핵심 키워드 '{keyword}'를 찾을 수 없습니다.")
        sys.exit(3)

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python verify_code.py [file_path] [keyword]")
        sys.exit(1)
    
    verify_code_integrity(sys.argv[1], sys.argv[2])