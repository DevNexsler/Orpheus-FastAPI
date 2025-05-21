import os
import tempfile
from supabase import create_client, Client
from supabase.lib.errors import StorageApiError # Import specific error
from dotenv import load_dotenv
from typing import Optional, Tuple
import uuid
import logging # Add logging

# Configure logger for this module
logger = logging.getLogger(__name__)
# Assuming logging.basicConfig is called in the main application (handler.py or app.py)

# Load environment variables
load_dotenv()

# Supabase configuration
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
SUPABASE_BUCKET_NAME = os.getenv("SUPABASE_BUCKET", "tts-audio")


class SupabaseStorageClient:
    """Client for handling Supabase Storage operations"""

    def __init__(self):
        """Initialize Supabase client configuration. Actual client connection is deferred."""
        if not SUPABASE_URL or not SUPABASE_KEY:
            logger.error("SUPABASE_URL and SUPABASE_KEY must be set in .env file or environment variables.")
            raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set.")
        
        self.supabase_url = SUPABASE_URL
        self.supabase_key = SUPABASE_KEY
        self.bucket_name = SUPABASE_BUCKET_NAME
        
        self.supabase: Optional[Client] = None
        self.initialized = False
        logger.info(f"SupabaseStorageClient config loaded. Bucket: {self.bucket_name}. Initialization deferred.")

    async def initialize_client(self):
        """Initializes the Supabase client and ensures the bucket exists. Idempotent."""
        if self.initialized and self.supabase:
            # logger.info("Supabase client already initialized.") # Too verbose for every call
            return

        logger.info("Initializing Supabase client...")
        try:
            self.supabase = create_client(self.supabase_url, self.supabase_key)
            if not self.supabase:
                # This case is unlikely as create_client usually returns a Client object or raises an error.
                raise ConnectionError("Failed to create Supabase client instance (create_client returned None).")
            
            logger.info("Supabase client object created. Ensuring bucket exists...")
            await self._ensure_bucket_exists()
            self.initialized = True
            logger.info(f"Supabase client initialized successfully for bucket '{self.bucket_name}'.")
        except Exception as e:
            logger.error(f"Failed to initialize Supabase client or ensure bucket '{self.bucket_name}': {e}", exc_info=True)
            self.initialized = False 
            self.supabase = None
            raise 

    async def _ensure_bucket_exists(self):
        """Ensures the configured bucket exists, creating it if necessary."""
        if not self.supabase:
            logger.error("Supabase client not available for _ensure_bucket_exists. Call initialize_client first.")
            raise ConnectionError("Supabase client not initialized.")

        try:
            logger.info(f"Checking for bucket: '{self.bucket_name}'")
            buckets_response = await self.supabase.storage.list_buckets()
            
            # supabase-py list_buckets() returns a list of Bucket objects or raises an error.
            # No need to check buckets_response for None typically.
            
            if not any(b.name == self.bucket_name for b in buckets_response):
                logger.info(f"Bucket '{self.bucket_name}' not found. Creating now with public access...")
                await self.supabase.storage.create_bucket(self.bucket_name, {"public": True})
                logger.info(f"Bucket '{self.bucket_name}' created successfully.")
            else:
                logger.info(f"Bucket '{self.bucket_name}' already exists.")
        except StorageApiError as e:
            error_message_str = str(e.message) if hasattr(e, 'message') else str(e) # e.message can be dict or str
            error_json = e.json() if hasattr(e, 'json') and callable(e.json) else {}

            already_exists_conditions = (
                "Bucket already exists" in error_message_str or
                (isinstance(e.message, dict) and e.message.get("error") == "Duplicate" and e.message.get("message") == "The resource already exists") or
                (error_json and error_json.get("error") == "Duplicate" and error_json.get("message") == "The resource already exists") or
                (error_json and error_json.get("error") == "409" and "already exists" in error_json.get("message", "").lower()) 
            )

            if already_exists_conditions:
                logger.info(f"Bucket '{self.bucket_name}' already exists (confirmed by StorageApiError: {error_message_str}).")
            elif "body/name must be string" in error_message_str:
                 logger.error(f"Supabase API Error during bucket operation for '{self.bucket_name}': {error_message_str}. This might indicate an issue with the bucket name itself or request format.")
                 raise 
            else:
                logger.error(f"Supabase StorageApiError while checking/creating bucket '{self.bucket_name}': {error_message_str}", exc_info=True)
                raise
        except Exception as e:
            logger.error(f"Unexpected error while checking/creating bucket '{self.bucket_name}': {e}", exc_info=True)
            raise

    async def upload_file(self, local_file_path: str, supabase_upload_path: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Uploads a file to Supabase Storage.

        Args:
            local_file_path: Path to the local file to upload.
            supabase_upload_path: Desired path/name for the file in Supabase.

        Returns:
            A tuple (public_url, error_message). error_message is None on success.
        """
        try:
            await self.initialize_client() 
        except Exception as e:
            logger.error(f"Failed to initialize Supabase client before upload: {e}", exc_info=True)
            return None, f"Supabase client initialization failed: {str(e)}"
            
        if not self.supabase or not self.initialized:
             logger.error("Cannot upload file: Supabase client is not properly initialized.")
             return None, "Supabase client not initialized"

        try:
            logger.info(f"Attempting to upload '{local_file_path}' to Supabase bucket '{self.bucket_name}' as '{supabase_upload_path}'")
            
            with open(local_file_path, "rb") as f:
                # Using upsert=True to overwrite if exists, or create if not.
                upload_response = await self.supabase.storage.from_(self.bucket_name).upload(
                    path=supabase_upload_path,
                    file=f,
                    file_options={"cache-control": "3600", "upsert": True} 
                )
            
            # supabase-py v2 upload response is just {'Key': 'path/to/file/in/bucket'} on success (200 OK)
            # or raises StorageApiError on failure. The response itself doesn't contain much detail on 200.
            logger.info(f"Upload attempt for '{supabase_upload_path}' completed.")

            # After successful upload, get the public URL
            public_url_data = self.supabase.storage.from_(self.bucket_name).get_public_url(supabase_upload_path)
            
            # In supabase-py v2, get_public_url returns the URL string directly.
            if isinstance(public_url_data, str) and public_url_data.startswith("http"):
                logger.info(f"Successfully uploaded '{supabase_upload_path}'. Public URL: {public_url_data}")
                return public_url_data, None
            else:
                # This case should ideally not happen if upload was successful and path is correct.
                logger.error(f"File uploaded to '{supabase_upload_path}' but failed to get a valid public URL. URL data: {public_url_data}")
                return None, "File uploaded but failed to retrieve a valid public URL."

        except StorageApiError as e:
            error_message_str = str(e.message) if hasattr(e, 'message') else str(e)
            logger.error(f"Supabase StorageApiError during upload of '{supabase_upload_path}': {error_message_str}", exc_info=True)
            return None, f"Storage API Error: {error_message_str}"
        except FileNotFoundError:
            logger.error(f"Local file not found for upload: {local_file_path}")
            return None, f"Local file not found: {local_file_path}"
        except Exception as e:
            logger.error(f"Unexpected error during upload of '{supabase_upload_path}': {e}", exc_info=True)
            return None, f"Unexpected error: {str(e)}"

    async def close(self):
        """Closes the Supabase client connection if it was established and supports async close."""
        if self.supabase and hasattr(self.supabase, 'aclose') and callable(self.supabase.aclose):
            try:
                await self.supabase.aclose()
                logger.info("Supabase async client session closed.")
            except Exception as e:
                logger.error(f"Error closing Supabase async client session: {e}", exc_info=True)
        elif self.supabase and hasattr(self.supabase, 'close') and callable(self.supabase.close): # Fallback for sync close
             try:
                self.supabase.close() # type: ignore
                logger.info("Supabase sync client session closed (fallback).")
             except Exception as e:
                logger.error(f"Error closing Supabase sync client session (fallback): {e}", exc_info=True)
        
        self.supabase = None
        self.initialized = False
        logger.info("Supabase client marked as closed/uninitialized.")

# Singleton instance
supabase_client = SupabaseStorageClient() 