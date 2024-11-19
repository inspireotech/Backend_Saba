from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from junglescout import Client
from junglescout.models.parameters.marketplace import Marketplace
from dotenv import load_dotenv
import os
import base64
import json


# Load environment variables
load_dotenv()

app = FastAPI()

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Adjust this to specific origins for better security
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Jungle Scout and Google Sheets configuration
API_KEY_NAME = os.getenv("API_KEY_NAME")
API_KEY = os.getenv("API_KEY")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME")
ENCODED_CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS")

if not API_KEY_NAME or not API_KEY or not GOOGLE_SHEET_NAME or not ENCODED_CREDENTIALS:
    raise RuntimeError("One or more required environment variables are missing!")

MARKETPLACE = Marketplace.US
client = Client(api_key_name=API_KEY_NAME, api_key=API_KEY, marketplace=MARKETPLACE)

# Decode credentials directly in memory
def decode_credentials():
    decoded_json = base64.b64decode(ENCODED_CREDENTIALS).decode("utf-8")
    return json.loads(decoded_json)

# Log buffer
log_buffer: List[str] = []

def log_message(message: str):
    """Helper to add logs to the log buffer."""
    log_buffer.append(message)
    print(message)  # Still print to console for debugging

@app.get("/logs")
def get_logs():
    """Endpoint to fetch the logs."""
    global log_buffer
    logs_to_return = log_buffer[:]
    log_buffer = []  # Clear logs after sending
    return {"logs": logs_to_return}

class AutomationRequest(BaseModel):
    update_mode: str  # "all" or "new"

# Helper function to connect to Google Sheets
def connect_to_google_sheets():
    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
    credentials_json = decode_credentials()
    creds = ServiceAccountCredentials.from_json_keyfile_dict(credentials_json, scope)
    client_gs = gspread.authorize(creds)
    sheet = client_gs.open(GOOGLE_SHEET_NAME).sheet1
    return sheet

# Helper function for safe attribute fetching
def safe_getattr(obj, attr, default=0):
    value = getattr(obj, attr, default)
    return value if isinstance(value, (int, float, str)) else default

# Helper function to check if a row is empty
def is_row_empty(row_data):
    return all(not cell for cell in row_data[1:])  # Skip the 'Keyword' column

# Fetch keyword insights and update Google Sheets
def fetch_keyword_insights(sheet, keyword, row_index):
    try:
        response = client.keywords_by_keyword(search_terms=keyword, marketplace=MARKETPLACE)
        if not response.data:
            log_message(f"No keyword insight data returned for: {keyword}")
            return

        keyword_data = response.data[0].attributes
        sheet.update(f'B{row_index}', [[
            safe_getattr(keyword_data, 'monthly_search_volume_exact'),
            safe_getattr(keyword_data, 'monthly_search_volume_broad'),
            safe_getattr(keyword_data, 'ease_of_ranking_score'),
            safe_getattr(keyword_data, 'sponsored_product_count'),
            None, None, None, None,  # Placeholder for product data
            None, None, None, None,
            None, None, None, None,
            safe_getattr(keyword_data, 'monthly_trend'),
            safe_getattr(keyword_data, 'quarterly_trend'),
            safe_getattr(keyword_data, 'recommended_promotions'),
            safe_getattr(keyword_data, 'sp_brand_ad_bid'),
            safe_getattr(keyword_data, 'ppc_bid_broad'),
            safe_getattr(keyword_data, 'ppc_bid_exact'),
            safe_getattr(keyword_data, 'estimated_30_day_search_volume')
        ]])
        log_message(f"Keyword insight data for '{keyword}' updated in Google Sheet.")

    except Exception as e:
        log_message(f"Error fetching keyword insights for {keyword}: {e}")

# Fetch product data and update Google Sheets
def fetch_product_data(sheet, keyword, row_index):
    try:
        response = client.product_database(include_keywords=[keyword], marketplace=MARKETPLACE)
        if not response.data:
            log_message(f"No product database data returned for keyword: {keyword}")
            return

        total_sales, total_revenue, total_price, total_reviews = 0, 0, 0, 0
        highest_price, highest_price_units = 0, 0
        second_highest_price, second_highest_units = 0, 0
        third_highest_price, third_highest_units = 0, 0
        sellers_with_low_reviews = 0
        irrelevant_listings = 0
        count = 0

        for product in response.data:
            attributes = product.attributes
            units_sold = safe_getattr(attributes, 'approximate_30_day_units_sold', 0)
            revenue = safe_getattr(attributes, 'approximate_30_day_revenue', 0)
            price = safe_getattr(attributes, 'price', 0)
            reviews = safe_getattr(attributes, 'reviews', 0)

            total_sales += units_sold
            total_revenue += revenue
            total_price += price
            total_reviews += reviews
            count += 1

            if units_sold >= 100:
                if price > highest_price:
                    third_highest_price, third_highest_units = second_highest_price, second_highest_units
                    second_highest_price, second_highest_units = highest_price, highest_price_units
                    highest_price, highest_price_units = price, units_sold
                elif price > second_highest_price:
                    third_highest_price, third_highest_units = second_highest_price, second_highest_units
                    second_highest_price, second_highest_units = price, units_sold
                elif price > third_highest_price:
                    third_highest_price, third_highest_units = price, units_sold

            if reviews < 100:
                sellers_with_low_reviews += 1

        avg_sales = total_sales / count if count else 0
        avg_revenue = total_revenue / count if count else 0
        avg_price = total_price / count if count else 0
        avg_reviews = total_reviews / count if count else 0

        sheet.update(f'F{row_index}:Q{row_index}', [[
            avg_sales, avg_revenue, avg_price, avg_reviews,
            highest_price, highest_price_units,
            second_highest_price, second_highest_units,
            third_highest_price, third_highest_units,
            sellers_with_low_reviews, irrelevant_listings
        ]])
        log_message(f"Product data for '{keyword}' updated in Google Sheet.")

    except Exception as e:
        log_message(f"Error fetching product data for {keyword}: {e}")

@app.post("/run-automation")
def run_automation(request: AutomationRequest):
    try:
        sheet = connect_to_google_sheets()
        rows = sheet.get_all_values()
        data_rows = rows[1:]  # Skip the headers

        if request.update_mode == "all":
            log_message("Processing all rows...")
            for idx, row in enumerate(data_rows, start=2):
                if row[0]:
                    fetch_keyword_insights(sheet, row[0], idx)
                    fetch_product_data(sheet, row[0], idx)
        elif request.update_mode == "new":
            log_message("Processing new rows...")
            for idx, row in enumerate(data_rows, start=2):
                if row[0] and is_row_empty(row):
                    fetch_keyword_insights(sheet, row[0], idx)
                    fetch_product_data(sheet, row[0], idx)
        else:
            raise HTTPException(status_code=400, detail="Invalid update mode")

        return {"status": "Automation completed successfully"}

    except Exception as e:
        log_message(f"Error in automation: {e}")
        raise HTTPException(status_code=500, detail="Internal Server Error")
