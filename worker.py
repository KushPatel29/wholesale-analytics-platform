import os
import time
import logging
from datetime import datetime
from sqlalchemy import create_engine, func, Column, Integer, DateTime, String
from sqlalchemy.orm import sessionmaker, declarative_base
import requests

# --- Configuration ---
# Use environment variables for sensitive/dynamic config
DB_USER = os.getenv("DB_USER", "wholesale-analytics")
DB_PASS = os.getenv("DB_PASS", "change_this_strong_password")
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_NAME = os.getenv("DB_NAME", "wholesale-analytics_db")
DATABASE_URI = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}/{DB_NAME}"

# Replace with the actual API endpoint
SOURCE_API_URL = os.getenv("SOURCE_API_URL", "https://api.example.com/data")

# Logging Setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# --- Database Setup ---
Base = declarative_base()

class AnalyticsData(Base):
    __tablename__ = 'analytics_data'
    id = Column(Integer, primary_key=True)
    updated_at = Column(DateTime, nullable=False, index=True)
    raw_data = Column(String) 

def get_db_engine():
    return create_engine(DATABASE_URI)

def init_db(engine):
    Base.metadata.create_all(engine)

# --- Core Logic ---
def get_start_date(session):
    """Determine fetch start date based on DB state."""
    max_date = session.query(func.max(AnalyticsData.updated_at)).scalar()
    if max_date:
        logger.info(f"Warm Start: Found data up to {max_date}")
        return max_date
    else:
        # Cold Start: Jan 1, 2018
        cold_start = datetime(2018, 1, 1)
        logger.info(f"Cold Start: No data found. Starting from {cold_start}")
        return cold_start

def fetch_and_load(session):
    """Main ETL step."""
    start_date = get_start_date(session)
    
    # Adjust parameters to match your actual API
    params = {
        "updated_after": start_date.isoformat(),
        "limit": 1000
    }

    logger.info(f"Fetching data from API since {start_date}...")
    
    try:
        resp = requests.get(SOURCE_API_URL, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.error(f"API Request Failed: {e}")
        raise e

    if not data:
        logger.info("No new data received.")
        return

    # Bulk insert logic
    new_records = []
    for item in data:
        # Ensure your API response actually has 'updated_at'
        updated_at_str = item.get('updated_at')
        if not updated_at_str:
            logger.warning(f"Skipping item without updated_at: {item}")
            continue
            
        record = AnalyticsData(
            updated_at=datetime.fromisoformat(updated_at_str),
            raw_data=str(item)
        )
        new_records.append(record)
    
    if new_records:
        session.add_all(new_records)
        session.commit()
        logger.info(f"Successfully inserted {len(new_records)} records.")

def run_worker():
    engine = get_db_engine()
    init_db(engine)
    Session = sessionmaker(bind=engine)
    
    logger.info("Worker started.")
    while True:
        session = Session()
        try:
            fetch_and_load(session)
            # Sleep between successful fetches
            time.sleep(60) 
        except Exception as e:
            logger.error(f"Worker cycle failed: {e}")
            session.rollback()
            logger.info("Sleeping for 60 seconds before retry...")
            time.sleep(60)
        finally:
            session.close()

if __name__ == "__main__":
    run_worker()
