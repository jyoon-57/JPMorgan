import sys
import os
import json
from pathlib import Path
from pprint import pprint

# Add project root to sys.path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from src.data.kis_collector import KisAuth, KisData
from dotenv import load_dotenv

def main():
    print("="*50)
    print("🚀 KIS API Connection & Data Verification")
    print("="*50)
    
    # Load env explicitly if needed, though KisAuth loads it via os.getenv
    # It assumes load_dotenv was called or variables are set.
    # KisAuth uses os.getenv, so we should ensure .env is loaded.
    env_path = Path(__file__).resolve().parent.parent / ".env"
    load_dotenv(env_path)

    try:
        # 1. Authentication
        print("\n🔑 [1. Authentication]")
        auth = KisAuth()
        auth.auth()
        print(f"✅ Success! Token expires at: {auth.token_expiry}")
        
        collector = KisData(auth)
        
        # 2. Market Index (KOSPI)
        print("\n📈 [2. KOSPI Index]")
        kospi = collector.get_market_index(market_code="0001")
        if kospi and str(kospi.get('rt_cd')) == '0':
            # Structure depends on specific API response
            # inquire-daily-index-chartprice: output1 (chart), output2 (basic info?)
            # Actually strictly 'inquire-daily-index-chartprice' returns array of daily candles in output2 presumably?
            # Let's inspect raw keys
            print(f"✅ Fetch Success. Response Keys: {list(kospi.keys())}")
            # Try to print current price if available in this endpoint response
            # If using daily chart, it might be a list.
            pprint(kospi.get('output1', {})[:1] if isinstance(kospi.get('output1'), list) else kospi.get('output1'))
        else:
            print(f"❌ Fetch Failed: {kospi}")
            
        # 3. Investor Trend
        print("\n👥 [3. Investor Trend]")
        investor = collector.get_investor_trend(market_code="0001")
        if investor and str(investor.get('rt_cd')) == '0':
            print("✅ Fetch Success.")
            pprint(investor.get('output', [])[:1] if isinstance(investor.get('output'), list) else investor.get('output'))
        else:
            print(f"❌ Fetch Failed: {investor}")
            
        # 4. Exchange Rate
        print("\n💵 [4. Exchange Rate (Fallback/KIS)]")
        rate = collector.get_exchange_rate()
        print(f"✅ Exchange Rate: {rate}")
        
    except Exception as e:
        print(f"\n❌ FATAL ERROR: {e}")

if __name__ == "__main__":
    main()
