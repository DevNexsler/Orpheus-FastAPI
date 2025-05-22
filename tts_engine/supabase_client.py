import os
import tempfile
from supabase import create_client, Client
from supabase.lib.client_options import ClientOptions
from dotenv import load_dotenv
from typing import Optional, Tuple
import uuid
import logging

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
            # Consider using ClientOptions if specific retry logic or timeouts are needed later
            # opts = ClientOptions(postgrest_client_timeout=10, storage_client_timeout=10)
            # self.supabase = create_client(self.supabase_url, self.supabase_key, options=opts)
            self.supabase = create_client(self.supabase_url, self.supabase_key)
            if not self.supabase:
                raise ConnectionError("Failed to create Supabase client instance.")
            
            logger.info("Supabase client object created. Ensuring bucket exists...")
            self._ensure_bucket_exists()
            self.initialized = True
            logger.info(f"Supabase client initialized successfully for bucket '{self.bucket_name}'.")
        except Exception as e:
            # Use str(e) for a more general error message logging
            logger.error(f"Failed to initialize Supabase client or ensure bucket '{self.bucket_name}': {str(e)}", exc_info=True)
            self.initialized = False 
            self.supabase = None
            raise 

    def _ensure_bucket_exists(self):
        """Ensures the configured bucket exists, creating it if necessary."""
        if not self.supabase:
            logger.error("Supabase client not available for _ensure_bucket_exists. Call initialize_client first.")
            raise ConnectionError("Supabase client not initialized.")

        try:
            logger.info(f"Checking for bucket: '{self.bucket_name}'")
            # list_buckets is synchronous
            response = self.supabase.storage.list_buckets() 

            # Check if response is an error (common pattern in supabase-py for non-exception errors)
            # Older versions might return a list directly, or an object with an .error attribute.
            # Let's assume response is the list of buckets if successful, or an object with .error otherwise.
            # A more robust check would be to see if the response has an 'error' attribute or if it's an instance of a known error type.
            # For now, we rely on the fact that a successful call returns a list of Bucket objects.
            if isinstance(response, list):
                if not any(b.name == self.bucket_name for b in response):
                    logger.info(f"Bucket '{self.bucket_name}' not found. Creating now with public access...")
                    # create_bucket is synchronous
                    create_response = self.supabase.storage.create_bucket(self.bucket_name, {"public": True}) 
                    # Check create_response for errors
                    if hasattr(create_response, 'error') and create_response.error:
                        # Try to get a message from the error
                        msg = create_response.error.message if hasattr(create_response.error, 'message') else str(create_response.error)
                        logger.error(f"Failed to create bucket '{self.bucket_name}': {msg}")
                        raise Exception(f"Failed to create bucket: {msg}") # Propagate as a Python exception
                    logger.info(f"Bucket '{self.bucket_name}' created successfully.")
                else:
                    logger.info(f"Bucket '{self.bucket_name}' already exists.")
            elif hasattr(response, 'error') and response.error: # If list_buckets itself returned an error object
                msg = response.error.message if hasattr(response.error, 'message') else str(response.error)
                # Handle specific "already exists" case if it comes through list_buckets error structure
                if "already exist" in msg.lower(): # General check
                     logger.info(f"Bucket '{self.bucket_name}' already exists (confirmed by list_buckets error: {msg}).")
                else:
                    logger.error(f"Failed to list buckets: {msg}")
                    raise Exception(f"Failed to list buckets: {msg}")
            else:
                # Fallback for unexpected response type from list_buckets
                logger.error(f"Unexpected response type from list_buckets: {type(response)}. Content: {str(response)[:200]}")
                raise Exception("Unexpected response from Supabase while listing buckets.")

        except Exception as e:
            # This will catch errors from the checks above or other unexpected issues
            error_message_str = str(e)
            # The complex parsing for "already exists" might still be needed if the API error isn't caught cleanly above
            if "already exist" in error_message_str.lower(): # Simplified check
                logger.info(f"Bucket '{self.bucket_name}' already exists (confirmed by exception: {error_message_str}).")
            else:
                logger.error(f"Supabase Exception while checking/creating bucket '{self.bucket_name}': {error_message_str}", exc_info=True)
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
            logger.error(f"Failed to initialize Supabase client before upload: {str(e)}", exc_info=True)
            return None, f"Supabase client initialization failed: {str(e)}"
            
        if not self.supabase or not self.initialized:
             logger.error("Cannot upload file: Supabase client is not properly initialized.")
             return None, "Supabase client not initialized"

        try:
            logger.info(f"Attempting to upload '{local_file_path}' to Supabase bucket '{self.bucket_name}' as '{supabase_upload_path}'")
            
            with open(local_file_path, "rb") as f:
                # .upload() in supabase-py v1.x is synchronous. For v2.x, it might be async.
                # Assuming v1.x for now based on typical RunPod environments unless supabase-py v2 is explicitly installed.
                # If it's v2 and .upload is async, this needs `await`.
                # Let's check supabase-py version or assume sync first. If an error occurs here, we will know.
                upload_response = self.supabase.storage.from_(self.bucket_name).upload(
                    path=supabase_upload_path,
                    file=f,
                    file_options={"cache-control": "3600", "upsert": True} 
                )
            
            logger.info(f"Upload attempt for '{supabase_upload_path}' completed.")

            # Check upload_response for error
            if hasattr(upload_response, 'error') and upload_response.error:
                msg = upload_response.error.message if hasattr(upload_response.error, 'message') else str(upload_response.error)
                logger.error(f"Error during Supabase upload of '{supabase_upload_path}': {msg}")
                return None, f"Supabase Upload Error: {msg}"

            # get_public_url is synchronous
            public_url_data = self.supabase.storage.from_(self.bucket_name).get_public_url(supabase_upload_path)
            
            if isinstance(public_url_data, str) and public_url_data.startswith("http"):
                logger.info(f"Successfully uploaded '{supabase_upload_path}'. Public URL: {public_url_data}")
                return public_url_data, None
            else:
                logger.error(f"File uploaded to '{supabase_upload_path}' but failed to get a valid public URL. URL data: {public_url_data}")
                return None, "File uploaded but failed to retrieve a valid public URL."

        except FileNotFoundError:
            logger.error(f"Local file not found for upload: {local_file_path}")
            return None, f"Local file not found: {local_file_path}"
        except Exception as e:
            error_message_str = str(e)
            logger.error(f"Supabase Exception during upload of '{supabase_upload_path}': {error_message_str}", exc_info=True)
            return None, f"Supabase Exception: {error_message_str}"

    async def close(self):
        """Closes the Supabase client connection if it was established and supports async close."""
        if self.supabase:
            # supabase-py client does not have an explicit close() or aclose() method for the entire client.
            # Connections are typically managed per request or by the underlying HTTP library.
            # We can just reset our internal state.
            pass 
        self.supabase = None
        self.initialized = False
        logger.info("Supabase client marked as closed/uninitialized.")