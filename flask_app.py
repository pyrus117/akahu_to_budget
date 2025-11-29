"""
Script for syncing transactions from Akahu to YNAB and Actual Budget.
Also provides webhook endpoints for real-time transaction syncing.
Updated to use APScheduler for 4-hourly polling instead of webhooks.
"""

from contextlib import contextmanager
import os
import logging
import argparse
import signal
import sys
import requests
from actual import Actual
from apscheduler.schedulers.background import BackgroundScheduler # NEW IMPORT

# Import from our modules package
from modules.sync_handler import sync_to_ab, sync_to_ynab
from modules.account_mapper import load_existing_mapping
from modules.config import AKAHU_ENDPOINT, AKAHU_HEADERS
from modules.config import RUN_SYNC_TO_AB, RUN_SYNC_TO_YNAB
from modules.config import ENVs
from modules.webhook_handler import create_flask_app

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("app.log"),
        logging.StreamHandler()
    ]
)


@contextmanager
def get_actual_client():
    """Context manager that yields an Actual client if RUN_SYNC_TO_AB is True,
    or None otherwise.
    This is needed because actualpy only works with contextmanager
    """
    if RUN_SYNC_TO_AB:
        try:
            logging.info(f"Attempting to connect to Actual server at {ENVs['ACTUAL_SERVER_URL']}")
            
            # NOTE: This is where the decryption happens using the ACTUAL_ENCRYPTION_KEY
            with Actual(
                base_url=ENVs['ACTUAL_SERVER_URL'],
                password=ENVs['ACTUAL_PASSWORD'],
                file=ENVs['ACTUAL_SYNC_ID'],
            ) as client:
                logging.info(f"Connected to AB: {client}")
                yield client
        except requests.exceptions.RequestException as e:
            logging.error(f"Failed to connect to Actual server: {str(e)}")
            if hasattr(e, 'response') and e.response is not None:
                logging.error(f"Response status: {e.response.status_code}")
                logging.error(f"Response headers: {dict(e.response.headers)}")
                logging.error(f"Response content: {e.response.text[:500]}")
            raise RuntimeError(f"Failed to connect to Actual server: {str(e)}") from None
    else:
        yield None


# Create and export the Flask app for WSGI
def signal_handler(sig, frame):
    logging.info("Received signal to terminate. Shutting down gracefully...")
    # Attempt to shut down the scheduler if it's running
    try:
        if 'scheduler' in globals() and scheduler.running:
            scheduler.shutdown(wait=False)
    except NameError:
        pass # Scheduler was not initialized
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)  # Handle Ctrl+C
signal.signal(signal.SIGTERM, signal_handler)  # Handle kill

# Global variable to hold the scheduler instance (for access in signal_handler)
scheduler = None

def start_scheduler():
    """Initializes and starts the APScheduler for periodic sync."""
    global scheduler
    logging.info("Initializing APScheduler for 4-hourly polling sync.")
    scheduler = BackgroundScheduler()
    
    # Schedule the run_sync function to run every 4 hours
    scheduler.add_job(
        func=run_sync, 
        trigger='interval', 
        hours=4,
        misfire_grace_time=600, # Allow 10 minutes for startup/misfire
        max_instances=1,
        id='akahu_polling_sync'
    )
    
    # Run a sync immediately upon starting the scheduler
    scheduler.add_job(
        func=run_sync,
        trigger='date',
        run_date='now',
        id='akahu_initial_sync'
    )
    
    scheduler.start()
    logging.info("Polling scheduler started. Sync runs now and every 4 hours.")


def create_application():
    """Create Flask application."""
    _, _, _, mapping_list = load_existing_mapping()
    
    # NOTE: The actual_client yielded here is short-lived as the context manager exits.
    # The webhook handler must not rely on it being persistently connected.
    # The scheduled job (run_sync) handles its own connection.
    with get_actual_client() as actual_client:
        app = create_flask_app(actual_client, mapping_list, {
            'AKAHU_PUBLIC_KEY': os.getenv('AKAHU_PUBLIC_KEY', ''),  # RFU (Reserved For Future Use)
            'akahu_endpoint': AKAHU_ENDPOINT,
            'akahu_headers': AKAHU_HEADERS
        })
        return app


def run_sync(account_ids=None, debug_mode=None):
    """Run sync operations directly.
    
    Args:
        account_ids (list[str], optional): List of Akahu account IDs to sync. If None, all accounts will be synced.
        debug_mode (str, optional): Debug mode setting. 'all' to print all transaction IDs, or a specific Akahu transaction ID for verbose debugging.
    """
    logging.info("Starting direct sync (Scheduled Polling Job)...")
    actual_count = ynab_count = 0

    _, _, _, mapping_list = load_existing_mapping()
    
    if account_ids:
        # Filter mapping_list to only include specified accounts
        filtered_mapping = {k: v for k, v in mapping_list.items() if k in account_ids}
        if not filtered_mapping:
            logging.warning(f"No matching accounts found for IDs: {account_ids}")
            return
        logging.info(f"Syncing specific accounts: {', '.join(account_ids)}")
        mapping_list = filtered_mapping

    with get_actual_client() as actual_client:
        if RUN_SYNC_TO_AB and actual_client:
            actual_client.download_budget()
            actual_count = sync_to_ab(actual_client, mapping_list, debug_mode=debug_mode)
            logging.info(f"Synced {actual_count} accounts to Actual Budget.")

        if RUN_SYNC_TO_YNAB:
            ynab_count = sync_to_ynab(mapping_list, debug_mode=debug_mode)
            logging.info(f"Synced {ynab_count} accounts to YNAB.")

    logging.info(f"Sync completed. Actual count: {actual_count}, YNAB count: {ynab_count}")


# Create and expose the Flask application for WSGI if not running in sync mode
application = None
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the Flask app or perform direct sync.")
    parser.add_argument("--sync", action="store_true", help="Perform direct sync and exit.")
    parser.add_argument("--accounts", help="Comma-separated list of Akahu account IDs to sync (e.g. acc_123,acc_456). If not provided, all accounts will be synced.")
    parser.add_argument("--debug", nargs='?', const='all', help="Enable debug mode. Without parameter, prints Akahu IDs for all transactions. With parameter, treats it as an Akahu transaction ID and enables verbose debugging for that transaction.")
    args = parser.parse_args()

    if args.sync:
        account_ids = args.accounts.split(',') if args.accounts else None
        run_sync(account_ids, debug_mode=args.debug)
    else:
        # 1. Start the polling scheduler
        start_scheduler()
        
        # 2. Create and run the Flask application (for the status/manual sync endpoints)
        application = create_application()
        development_mode = os.getenv('FLASK_ENV') == 'development'
        application.run(host="0.0.0.0", port=5000, debug=development_mode)
else:
    # For WSGI deployment, create the application and start the scheduler
    start_scheduler()
    application = create_application()
