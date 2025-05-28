import runpod
import os
import time
import logging
import base64
from datetime import datetime
import sys # Added for print flushing
import uuid # For unique filenames
from typing import Optional, Dict, Any, Tuple # For type hinting

# --- EARLY ENVIRONMENT VARIABLE LOADING ---
# Load .env file and OS environment variables before other imports
# This is crucial for tts_engine.inference to pick them up.
from dotenv import load_dotenv
load_dotenv(override=True) # override=True ensures OS vars can override .env vars if both exist
print("---HANDLER.PY: load_dotenv() called.---", flush=True)

# --- Setup logging (retained for good practice, but we'll add prints) ---
logging.basicConfig(level=logging.INFO) # Configure logging early
logger = logging.getLogger(__name__)
print("---HANDLER.PY: Basic logging configured.---", flush=True)

# --- CRITICAL IMPORTS AND INITIALIZATION WITH DETAILED ERROR LOGGING ---
print("---HANDLER.PY: Attempting critical imports and TTS Engine initialization...---", flush=True)
# Use string literal for type hint to avoid NameError before import
supabase_client_instance: Optional["SupabaseStorageClient"] = None # Declare for later use

try:
    # Assuming tts_engine and its components are in the PYTHONPATH
    from tts_engine import generate_speech_from_api, AVAILABLE_VOICES, DEFAULT_VOICE
    print("---HANDLER.PY: Successfully imported from tts_engine.---", flush=True)
    
    # Import Supabase client
    from tts_engine.supabase_client import SupabaseStorageClient
    print("---HANDLER.PY: Successfully imported SupabaseStorageClient.---", flush=True)

    # The SNAC model is loaded by speechpipe.py when it's imported.
    # We just need to ensure tts_engine.speechpipe is imported.
    import tts_engine.speechpipe # This should trigger the model load
    logger.info("TTS Engine initialized (SNAC model should be loaded/downloaded).")
    print("---HANDLER.PY: TTS Engine initialized (SNAC model should be loaded/downloaded via speechpipe import).---", flush=True)

except Exception as e:
    import traceback
    tb_str = traceback.format_exc()
    # Log to both logger and print for maximum visibility
    logger.error(f"---HANDLER.PY: CRITICAL STARTUP ERROR during imports or TTS init: {e} ---", exc_info=True)
    print(f"---HANDLER.PY: CRITICAL STARTUP ERROR during imports or TTS init: {e} ---", flush=True)
    print(f"---HANDLER.PY: TRACEBACK: {tb_str} ---", flush=True)
    # If model loading fails, the handler won't work.
    # Reraise the exception to indicate a fatal startup error, which will cause worker exit.
    raise e

# --- Supabase Client Global Instance (already declared, just for clarity) ---
# supabase_client_instance: Optional[SupabaseStorageClient] = None 
# This is already declared above before the try-except block.

async def get_supabase_client() -> SupabaseStorageClient:
    """Gets or initializes the Supabase client. Ensures it's initialized before returning."""
    global supabase_client_instance
    if supabase_client_instance is None or not supabase_client_instance.initialized:
        logger.info("Supabase client instance not found or not initialized. Creating/Initializing...")
        # Ensure SupabaseStorageClient was successfully imported before trying to instantiate
        if 'SupabaseStorageClient' not in globals() and 'SupabaseStorageClient' not in locals():
             # This case should ideally be caught by the import error block above,
             # but as a safeguard:
             err_msg = "SupabaseStorageClient class not available due to earlier import failure."
             logger.error(f"---GET_SUPABASE_CLIENT: {err_msg} ---")
             raise ImportError(err_msg)
             
        supabase_client_instance = SupabaseStorageClient() # Creates the config
        try:
            await supabase_client_instance.initialize_client() # Actually connects and checks bucket
            logger.info("Supabase client successfully initialized.")
        except Exception as e:
            logger.error(f"Failed to initialize Supabase client during get_supabase_client: {e}", exc_info=True)
            # Depending on desired behavior, you might want to set supabase_client_instance to None or re-raise
            # For now, it will be non-None but not initialized, subsequent checks might retry.
            raise # Re-raise the exception to make the handler fail if Supabase isn't up
    return supabase_client_instance

async def tts_handler(job: Dict[str, Any]) -> Dict[str, Any]:
    """Handles TTS requests, generates speech, optionally uploads to Supabase, and returns audio."""
    job_id = job.get('id', 'unknown_job')
    logger.info(f"---TTS_HANDLER [{job_id}]: Received job.---")
    logger.debug(f"---TTS_HANDLER [{job_id}]: Full job object: {job}---")

    job_input = job.get('input')
    if not job_input:
        logger.error(f"---TTS_HANDLER [{job_id}]: No input found in job.---")
        return {"error": "No input provided", "status": "FAILED"}
        
    logger.debug(f"---TTS_HANDLER [{job_id}]: Job input: {job_input}---")

    text_to_speak = job_input.get("input")
    voice = job_input.get("voice", DEFAULT_VOICE)
    store_in_supabase = job_input.get("store_in_supabase", False)
    output_format = job_input.get("output_format", "wav") # Default to wav
    # further params like sample_rate, model can be extracted if tts_engine supports them

    if not text_to_speak:
        logger.error(f"---TTS_HANDLER [{job_id}]: 'input' field (text to speak) is missing or empty.---")
        return {"error": "Input text is missing or empty", "status": "FAILED"}

    if voice not in AVAILABLE_VOICES:
        logger.warning(f"---TTS_HANDLER [{job_id}]: Voice '{voice}' not available. Falling back to default: {DEFAULT_VOICE}.---")
        voice = DEFAULT_VOICE

    logger.info(f"---TTS_HANDLER [{job_id}]: Synthesizing for text: '{text_to_speak[:50]}...' with voice: {voice}, store_in_supabase: {store_in_supabase}, format: {output_format}---")

    # Create a unique filename for the temporary output file
    # Using job_id and a timestamp for better uniqueness if jobs can be concurrent
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    unique_id = uuid.uuid4().hex[:8]
    base_filename = f"handler_{voice}_{job_id}_{timestamp}_{unique_id}"
    temp_output_filename = f"{base_filename}.{output_format}"
    
    # Ensure outputs directory exists
    os.makedirs("outputs", exist_ok=True)
    temp_output_path = os.path.join("outputs", temp_output_filename)
    logger.debug(f"---TTS_HANDLER [{job_id}]: Temp output path: {temp_output_path}---")

    start_time = time.time()
    try:
        # Note: generate_speech_from_api is assumed to be synchronous.
        # If it were async, we would await it.
        # For CPU-bound or blocking I/O in an async handler, consider run_in_executor.
        success, details = generate_speech_from_api(
            prompt=text_to_speak,
            voice=voice,
            output_file=temp_output_path,
            output_format=output_format # Pass output_format to the engine
            # Add other parameters like model, sample_rate if needed
        )
        generation_time = time.time() - start_time
        
        if not success:
            logger.error(f"---TTS_HANDLER [{job_id}]: Speech generation failed. Details: {details}---")
            return {"error": f"Speech generation failed: {details}", "status": "FAILED"}

        logger.info(f"---TTS_HANDLER [{job_id}]: Speech generated in {generation_time:.2f}s. Output at: {temp_output_path}---")

        if not os.path.exists(temp_output_path):
            logger.error(f"---TTS_HANDLER [{job_id}]: Generated audio file not found at {temp_output_path} despite success status.---")
            return {"error": "Generated audio file not found after successful generation.", "status": "FAILED"}
        
        file_size_bytes = os.path.getsize(temp_output_path)
        logger.info(f"---TTS_HANDLER [{job_id}]: Generated file size: {file_size_bytes} bytes.---")
        
        response_payload: Dict[str, Any] = {
            "status": "ok",
            "voice": voice,
            "content_type": f"audio/{output_format}",
            "generation_time_seconds": round(generation_time, 2),
            "audio_file_size_bytes": file_size_bytes
        }

        if store_in_supabase:
            logger.info(f"---TTS_HANDLER [{job_id}]: Attempting to upload {temp_output_path} to Supabase bucket.---")
            supabase_upload_path = f"audio/{temp_output_filename}" # Store in an 'audio' folder within the bucket
            
            s_client = None # Initialize to None
            try:
                s_client = await get_supabase_client()
            except Exception as e:
                logger.error(f"---TTS_HANDLER [{job_id}]: Supabase client initialization failed: {e}. Will encode to base64 instead.---", exc_info=True)
                # Fallback to base64 if client init fails

            if s_client and s_client.initialized:
                public_url, error_message = await s_client.upload_file(temp_output_path, supabase_upload_path)
                if public_url:
                    logger.info(f"---TTS_HANDLER [{job_id}]: Successfully uploaded to Supabase. URL: {public_url}---")
                    response_payload["supabase_url"] = public_url
                    response_payload["storage_type"] = "supabase"
                else:
                    logger.error(f"---TTS_HANDLER [{job_id}]: Supabase upload failed: {error_message}. Falling back to base64.---")
                    # Fallback to base64 if upload fails
                    with open(temp_output_path, "rb") as audio_file:
                        audio_base64 = base64.b64encode(audio_file.read()).decode('utf-8')
                    response_payload["audio_base64"] = audio_base64
                    response_payload["storage_type"] = "base64"
                    response_payload["upload_error"] = error_message
            else:
                 # Already logged failure to get/init client, proceed to base64
                logger.warning(f"---TTS_HANDLER [{job_id}]: Supabase client not available or not initialized. Proceeding with base64 encoding.---")
                with open(temp_output_path, "rb") as audio_file:
                    audio_base64 = base64.b64encode(audio_file.read()).decode('utf-8')
                response_payload["audio_base64"] = audio_base64
                response_payload["storage_type"] = "base64"

        else: # Not storing in Supabase, return base64
            logger.info(f"---TTS_HANDLER [{job_id}]: Encoding audio to base64 from {temp_output_path}.---")
            with open(temp_output_path, "rb") as audio_file:
                audio_base64 = base64.b64encode(audio_file.read()).decode('utf-8')
            response_payload["audio_base64"] = audio_base64
            response_payload["storage_type"] = "base64"
            logger.info(f"---TTS_HANDLER [{job_id}]: Audio encoded to base64. Size: {len(audio_base64)} chars.---")

        return response_payload

    except Exception as e:
        logger.error(f"---TTS_HANDLER [{job_id}]: Unhandled exception in tts_handler: {e}---", exc_info=True)
        return {"error": f"An unexpected error occurred: {str(e)}", "status": "FAILED"}
    finally:
        # Clean up the temporary file
        if os.path.exists(temp_output_path):
            try:
                os.remove(temp_output_path)
                logger.info(f"---TTS_HANDLER [{job_id}]: Removed temp file {temp_output_path}.---")
            except OSError as e:
                logger.error(f"---TTS_HANDLER [{job_id}]: Error removing temp file {temp_output_path}: {e}.---")
        
        # Attempt to close Supabase client if it was initialized
        # This is more relevant for long-lived workers, but good practice.
        # global supabase_client_instance # Redundant if already declared global earlier in function or module scope for assignment
        # if supabase_client_instance and supabase_client_instance.initialized:
        #     logger.info(f"---TTS_HANDLER [{job_id}]: Attempting to close Supabase client session.---")
        #     await supabase_client_instance.close() # This might be too aggressive if client is shared
        # We will rely on the get_supabase_client to manage the instance lifecycle for now.


# Start the RunPod serverless handler
logger.info("---HANDLER.PY: Attempting to start RunPod serverless handler...---")
print("---HANDLER.PY: Attempting to start RunPod serverless handler...---", flush=True) # Added flush
runpod.serverless.start({"handler": tts_handler})
print("---HANDLER.PY: runpod.serverless.start call completed (this line might not be reached if it blocks).---", flush=True) 