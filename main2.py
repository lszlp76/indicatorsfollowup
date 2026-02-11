from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from pydantic import BaseModel
import yfinance as yf
import pandas as pd
import uvicorn
import threading
import time
from datetime import datetime

# --- VERİ MODELLERİ ---
class AnalysisRequest(BaseModel):
    symbol: str
    rsi_interval: str = "1h"   # RSI ve MACD hesaplaması için (Örn: 15m)
    price_interval: str = "1h" # Fiyat değişim yüzdesi için (Örn: 1d)

# --- GLOBAL ÖNBELLEK ---
MARKET_CACHE = []
LAST_UPDATE = None
DEFAULT_SYMBOLS = ['THYAO.IS', 'GARAN.IS', 'BTC-USD', 'ETH-USD']

# --- ARKA PLAN GÖREVİ ---
def background_updater():
    global MARKET_CACHE, LAST_UPDATE
    print("🔄 Arka plan veri motoru çalıştırıldı (Varsayılan Hisseler)...")
    
    while True:
        try:
            temp_data = []
            for sym in DEFAULT_SYMBOLS:
                # Arka planda varsayılan olarak 1 Saatlik RSI ve 1 Günlük (1d) fiyat değişimi baz alalım
                data = process_stock_analysis(sym, rsi_interval="1h", price_interval="1d")
                if data:
                    temp_data.append(data)
            
            if temp_data:
                MARKET_CACHE = temp_data
                LAST_UPDATE = datetime.now()
                
        except Exception as e:
            print(f"⚠️ Arka Plan Hatası: {e}")
            
        time.sleep(30) 

@asynccontextmanager
async def lifespan(app: FastAPI):
    worker = threading.Thread(target=background_updater, daemon=True)
    worker.start()
    yield

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- ANALİZ MOTORU ---

def calculate_rsi(series, period=14):
    if len(series) < period + 1: return 50.0
    delta = series.diff(1)
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    # Wilder's Smoothing (TradingView benzeri)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    
    if avg_loss.iloc[-1] == 0: return 100.0
    
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calculate_macd(series, slow=26, fast=12, signal=9):
    exp1 = series.ewm(span=fast, adjust=False).mean()
    exp2 = series.ewm(span=slow, adjust=False).mean()
    macd = exp1 - exp2
    return macd

def determine_period(interval):
    """Interval'e göre ne kadar geçmiş veri çekeceğimizi belirler"""
    if interval in ['1m', '2m', '5m']: return '1d'
    if interval in ['15m', '30m', '90m']: return '5d'
    if interval in ['1h', '1d']: return '2mo'
    if interval in ['1wk', '1mo']: return '2y'
    return '2mo'

def get_historical_data(symbol, interval):
    """Belirli bir interval için veri çeker"""
    try:
        ticker = yf.Ticker(symbol)
        period = determine_period(interval)
        df = ticker.history(period=period, interval=interval, auto_adjust=True)
        return df
    except:
        return pd.DataFrame()

def process_stock_analysis(symbol, rsi_interval, price_interval):
    """
    RSI'yı rsi_interval'a göre,
    Fiyat Değişimini price_interval'a göre hesaplar ve birleştirir.
    """
    try:
        # 1. RSI ve MACD için Veri Çek (Teknik Analiz Verisi)
        df_tech = get_historical_data(symbol, rsi_interval)
        
        # Eğer yfinance 4h desteklemiyorsa manuel resample yapılabilir ama 
        # şimdilik temel intervaller üzerinden gidiyoruz.
        if df_tech.empty or len(df_tech) < 20: 
            # Veri yoksa veya yetersizse None dön
            return None

        # RSI ve MACD Hesapla
        rsi_val = calculate_rsi(df_tech['Close']).iloc[-1]
        macd_val = calculate_macd(df_tech['Close']).iloc[-1]
        
        # Güncel fiyat (Teknik analiz verisindeki son kapanış fiyatı - bu genellikle canlı fiyattır)
        current_price = float(df_tech['Close'].iloc[-1])

        # 2. Fiyat Değişimi Hesapla
        change_val = 0.0
        
        if rsi_interval == price_interval:
            # Eğer iki interval aynıysa, fazladan istek atmaya gerek yok
            # Bir önceki mumun kapanışına göre değişim
            prev_close = df_tech['Close'].iloc[-2] if len(df_tech) > 1 else current_price
            change_val = ((current_price - prev_close) / prev_close) * 100
        else:
            # Farklı interval ise (Örn: RSI 15m, Fiyat 1d)
            df_price = get_historical_data(symbol, price_interval)
            
            if not df_price.empty and len(df_price) > 1:
                # Price interval '1d' (günlük) ise:
                # iloc[-1] -> Bugün (canlı mum)
                # iloc[-2] -> Dün (kapanmış mum)
                # Değişimi dünkü kapanışa göre hesapla
                last_closed_candle = df_price['Close'].iloc[-2]
                change_val = ((current_price - last_closed_candle) / last_closed_candle) * 100
            else:
                # Fiyat verisi çekilemediyse 0 dön (veya RSI verisinden tahmini değişim)
                change_val = 0.0    
            print(f"✅ {symbol} analizi tamamlandı: Fiyat={current_price:.2f} price interval {price_interval}, RSI={rsi_val:.2f} interval {rsi_interval}, MACD={macd_val:.2f}, Değişim={change_val:.2f}%")
        return {
            "id": hash(symbol + rsi_interval + price_interval),
            "symbol": symbol.replace('.IS', '').replace('-USD', ''),
            "full_symbol": symbol,
            "price": current_price,
            "rsi": float(rsi_val) if not pd.isna(rsi_val) else 50.0,
            "macd": float(macd_val) if not pd.isna(macd_val) else 0.0,
            "change": float(change_val),
            "interval": rsi_interval,       # Bilgi amaçlı: RSI hangi grafiğe göre?
            "price_interval": price_interval # Bilgi amaçlı: Değişim hangi grafiğe göre?
        }

    except Exception as e:
        print(f"Process Error ({symbol}): {e}")
        return None

# --- ENDPOINTLER ---

@app.get("/")
def read_root():
    return {"status": "running"}

@app.post("/api/analyze")
def analyze_stock(request: AnalysisRequest):
    """Kullanıcının belirlediği İKİ AYRI intervale göre analiz yapar"""
    symbol = request.symbol.upper().strip()
    
    # Geçerli aralıklar
    valid_intervals = ['1m', '2m', '5m', '15m', '30m', '60m', '90m', '1h', '4h', '1d', '5d', '1wk', '1mo', '3mo', '1y', '5y']
    
    # Fallback kontrolleri (Geçersiz interval gelirse 1h yap)
    print("gelen rsi interval:", request.rsi_interval)
    rsi_int = request.rsi_interval if request.rsi_interval in valid_intervals else "1h"
    price_int = request.price_interval if request.price_interval in valid_intervals else "1h"

    print(f"🔍 Analiz İsteği: {symbol} | RSI: {rsi_int} | Fiyat: {price_int}")
    
    data = process_stock_analysis(symbol, rsi_interval=rsi_int, price_interval=price_int)
    
    if data:
        return {"status": "success", "data": data}
    else:
        raise HTTPException(status_code=404, detail="Veri bulunamadı.")

if __name__ == "__main__":
    print("\n🚀 BORSA API (V4 - Çift Interval Modu)")
    uvicorn.run(app, host="0.0.0.0", port=8001)