import os
import sys
import requests
import json
import time
import wave
import numpy as np
import argparse
import threading
import queue
import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Any, Optional, Generator, Union, Tuple, AsyncGenerator
from dotenv import load_dotenv

# Optional sounddevice import for local audio playback
try:
    import sounddevice as sd
    SOUNDDEVICE_AVAILABLE = True
except ImportError:
    print("Warning: sounddevice not available. Audio playback disabled.")
    SOUNDDEVICE_AVAILABLE = False

# Helper to detect if running in Uvicorn's reloader
def is_reloader_process():
    """Check if the current process is a uvicorn reloader"""
    return (sys.argv[0].endswith('_continuation.py') or 
            os.environ.get('UVICORN_STARTED') == 'true')

# Set a flag to avoid repeat messages
IS_RELOADER = is_reloader_process()
if not IS_RELOADER:
    os.environ['UVICORN_STARTED'] = 'true'

# Load environment variables from .env file
load_dotenv()

# Detect hardware capabilities and display information
import torch
import psutil

# Detect if we're on a high-end system based on hardware capabilities
HIGH_END_GPU = False
if torch.cuda.is_available():
    # Get GPU properties
    props = torch.cuda.get_device_properties(0)
    gpu_name = props.name
    gpu_mem_gb = props.total_memory / (1024**3)
    compute_capability = f"{props.major}.{props.minor}"
    
    # Consider high-end if: large VRAM (≥16GB) OR high compute capability (≥8.0) OR large VRAM (≥12GB) with good CC (≥7.0)
    HIGH_END_GPU = (gpu_mem_gb >= 16.0 or 
                    props.major >= 8 or 
                    (gpu_mem_gb >= 12.0 and props.major >= 7))
        
    if HIGH_END_GPU:
        if not IS_RELOADER:
            print(f"🖥️ Hardware: High-end CUDA GPU detected")
            print(f"📊 Device: {gpu_name}")
            print(f"📊 VRAM: {gpu_mem_gb:.2f} GB")
            print(f"📊 Compute Capability: {compute_capability}")
            print("🚀 Using high-performance optimizations")
    else:
        if not IS_RELOADER:
            print(f"🖥️ Hardware: CUDA GPU detected")
            print(f"📊 Device: {gpu_name}")
            print(f"📊 VRAM: {gpu_mem_gb:.2f} GB")
            print(f"📊 Compute Capability: {compute_capability}")
            print("🚀 Using GPU-optimized settings")
else:
    # Get CPU info
    cpu_cores = psutil.cpu_count(logical=False)
    cpu_threads = psutil.cpu_count(logical=True)
    ram_gb = psutil.virtual_memory().total / (1024**3)
    
    if not IS_RELOADER:
        print(f"🖥️ Hardware: CPU only (No CUDA GPU detected)")
        print(f"📊 CPU: {cpu_cores} cores, {cpu_threads} threads")
        print(f"📊 RAM: {ram_gb:.2f} GB")
        print("⚙️ Using CPU-optimized settings")

# Load configuration from environment variables without hardcoded defaults
# Critical settings - will log errors if missing
required_settings = ["ORPHEUS_API_URL", "ORPHEUS_API_KEY"]
missing_settings = [s for s in required_settings if s not in os.environ]
if missing_settings:
    print(f"ERROR: Missing required environment variable(s): {', '.join(missing_settings)}")
    print("Please set them in .env file or environment. See .env.example for defaults.")

# API connection settings
API_URL = os.environ.get("ORPHEUS_API_URL")
API_KEY = os.environ.get("ORPHEUS_API_KEY")

if not API_URL:
    print("WARNING: ORPHEUS_API_URL not set. API calls will fail until configured.")
if not API_KEY:
    print("WARNING: ORPHEUS_API_KEY not set. API calls will likely fail due to unauthorized access.")

HEADERS = {
    "Content-Type": "application/json"
}
if API_KEY:
    HEADERS["Authorization"] = f"Bearer {API_KEY}"

# Request timeout settings
try:
    REQUEST_TIMEOUT = int(os.environ.get("ORPHEUS_API_TIMEOUT", "120"))
except (ValueError, TypeError):
    print("WARNING: Invalid ORPHEUS_API_TIMEOUT value, using 120 seconds as fallback")
    REQUEST_TIMEOUT = 120

print(f"--- DEBUG: Initial REQUEST_TIMEOUT set to: {REQUEST_TIMEOUT} seconds ---")

# Model generation parameters from environment variables
try:
    MAX_TOKENS = int(os.environ.get("ORPHEUS_MAX_TOKENS", "8192"))
except (ValueError, TypeError):
    print("WARNING: Invalid ORPHEUS_MAX_TOKENS value, using 8192 as fallback")
    MAX_TOKENS = 8192

try:
    TEMPERATURE = float(os.environ.get("ORPHEUS_TEMPERATURE", "0.1"))
except (ValueError, TypeError):
    print("WARNING: Invalid ORPHEUS_TEMPERATURE value, using 0.1 as fallback")
    TEMPERATURE = 0.1

try:
    TOP_P = float(os.environ.get("ORPHEUS_TOP_P", "0.85"))
except (ValueError, TypeError):
    print("WARNING: Invalid ORPHEUS_TOP_P value, using 0.85 as fallback")
    TOP_P = 0.85

# Repetition penalty is hardcoded to 1.1 which is the only stable value for quality output
REPETITION_PENALTY = 1.1

try:
    SAMPLE_RATE = int(os.environ.get("ORPHEUS_SAMPLE_RATE", "24000"))
except (ValueError, TypeError):
    print("WARNING: Invalid ORPHEUS_SAMPLE_RATE value, using 24000 as fallback")
    SAMPLE_RATE = 24000

# Print loaded configuration only in the main process, not in the reloader
if not IS_RELOADER:
    print(f"Configuration loaded:")
    print(f"  API_URL: {API_URL}")
    if API_KEY:
        print(f"  API_KEY: {'Loaded (sensitive value not shown)' if API_KEY else 'Not Set'}")
    else:
        print(f"  API_KEY: Not Set")
    print(f"  MAX_TOKENS: {MAX_TOKENS}")
    print(f"  TEMPERATURE: {TEMPERATURE}")
    print(f"  TOP_P: {TOP_P}")
    print(f"  REPETITION_PENALTY: {REPETITION_PENALTY}")

# Parallel processing settings
NUM_WORKERS = 4 if HIGH_END_GPU else 2

# Define voices by language
ENGLISH_VOICES = ["tara", "leah", "jess", "leo", "dan", "mia", "zac", "zoe"]
FRENCH_VOICES = ["pierre", "amelie", "marie"]
GERMAN_VOICES = ["jana", "thomas", "max"]
KOREAN_VOICES = ["유나", "준서"]
HINDI_VOICES = ["ऋतिका"]
MANDARIN_VOICES = ["长乐", "白芷"]
SPANISH_VOICES = ["javi", "sergio", "maria"]
ITALIAN_VOICES = ["pietro", "giulia", "carlo"]

# Combined list for API compatibility
AVAILABLE_VOICES = (
    ENGLISH_VOICES + 
    FRENCH_VOICES + 
    GERMAN_VOICES + 
    KOREAN_VOICES + 
    HINDI_VOICES + 
    MANDARIN_VOICES + 
    SPANISH_VOICES + 
    ITALIAN_VOICES
)
DEFAULT_VOICE = "tara"  # Best voice according to documentation

# Map voices to languages for the UI
VOICE_TO_LANGUAGE = {}
VOICE_TO_LANGUAGE.update({voice: "english" for voice in ENGLISH_VOICES})
VOICE_TO_LANGUAGE.update({voice: "french" for voice in FRENCH_VOICES})
VOICE_TO_LANGUAGE.update({voice: "german" for voice in GERMAN_VOICES})
VOICE_TO_LANGUAGE.update({voice: "korean" for voice in KOREAN_VOICES})
VOICE_TO_LANGUAGE.update({voice: "hindi" for voice in HINDI_VOICES})
VOICE_TO_LANGUAGE.update({voice: "mandarin" for voice in MANDARIN_VOICES})
VOICE_TO_LANGUAGE.update({voice: "spanish" for voice in SPANISH_VOICES})
VOICE_TO_LANGUAGE.update({voice: "italian" for voice in ITALIAN_VOICES})

# Languages list for the UI
AVAILABLE_LANGUAGES = ["english", "french", "german", "korean", "hindi", "mandarin", "spanish", "italian"]

# Import the unified token handling from speechpipe
from .speechpipe import turn_token_into_id, CUSTOM_TOKEN_PREFIX

# Special token IDs for Orpheus model
START_TOKEN_ID = 128259
END_TOKEN_IDS = [128009, 128260, 128261, 128257]

# Performance monitoring
class PerformanceMonitor:
    """Track and report performance metrics"""
    def __init__(self):
        self.start_time = time.time()
        self.token_count = 0
        self.audio_chunks = 0
        self.last_report_time = time.time()
        self.report_interval = 2.0  # seconds
        
    def add_tokens(self, count: int = 1) -> None:
        self.token_count += count
        self._check_report()
        
    def add_audio_chunk(self) -> None:
        self.audio_chunks += 1
        self._check_report()
        
    def _check_report(self) -> None:
        current_time = time.time()
        if current_time - self.last_report_time >= self.report_interval:
            self.report()
            self.last_report_time = current_time
            
    def report(self) -> None:
        elapsed = time.time() - self.start_time
        if elapsed < 0.001:
            return
            
        tokens_per_sec = self.token_count / elapsed
        chunks_per_sec = self.audio_chunks / elapsed
        
        # Estimate audio duration based on audio chunks (each chunk is ~0.085s of audio)
        est_duration = self.audio_chunks * 0.085
        
        print(f"Progress: {tokens_per_sec:.1f} tokens/sec, est. {est_duration:.1f}s audio generated, {self.token_count} tokens, {self.audio_chunks} chunks in {elapsed:.1f}s")

# Create global performance monitor
perf_monitor = PerformanceMonitor()

def format_prompt(prompt: str, voice: str = DEFAULT_VOICE) -> str:
    """Format prompt for Orpheus model with voice prefix and special tokens."""
    # Validate voice and provide fallback
    if voice not in AVAILABLE_VOICES:
        print(f"Warning: Voice '{voice}' not recognized. Using '{DEFAULT_VOICE}' instead.")
        voice = DEFAULT_VOICE
        
    # Format similar to how engine_class.py does it with special tokens
    formatted_prompt = f"{voice}: {prompt}"
    
    # Add special token markers for the Orpheus-FASTAPI
    special_start = "<|audio|>"  # Using the additional_special_token from config
    special_end = "<|eot_id|>"   # Using the eos_token from config
    
    return f"{special_start}{formatted_prompt}{special_end}"

def generate_tokens_from_api(prompt: str, voice: str = DEFAULT_VOICE, temperature: float = TEMPERATURE, 
                           top_p: float = TOP_P, max_tokens: int = MAX_TOKENS, 
                           repetition_penalty: float = REPETITION_PENALTY) -> Generator[str, None, None]:
    """Generate tokens from text using RunPod API with proper JSON response handling."""
    start_time = time.time()
    formatted_prompt = format_prompt(prompt, voice)
    print(f"Generating speech for: {formatted_prompt}")
    
    # Optimize the token generation for GPUs
    if HIGH_END_GPU:
        print("Using optimized parameters for high-end GPU")
    elif torch.cuda.is_available():
        print("Using optimized parameters for GPU acceleration")
    
    # Create the request payload for the LLM server
    llm_input_payload = {
        "prompt": formatted_prompt,
        "temperature": temperature,
        "top_p": top_p,
        "max_tokens": max_tokens,
        "repetition_penalty": repetition_penalty,
        "stop": ["<|eot_id|>"],
        "stream": False  # Use synchronous mode since streaming endpoints aren't available
    }
    
    # Wrap the llm_input_payload under the "input" key for RunPod
    payload = {
        "input": llm_input_payload
    }
    
    # Session for connection pooling and retry logic
    session = requests.Session()
    
    retry_count = 0
    max_retries = 3
    
    print(f"--- DEBUG: REQUEST_TIMEOUT in generate_tokens_from_api: {REQUEST_TIMEOUT} seconds ---")
    
    while retry_count < max_retries:
        try:
            print(f"--- TTS Worker Debug ---")
            print(f"Attempting to POST to URL: {API_URL}")
            print(f"--- End TTS Worker Debug ---")
            
            # Use the /runsync endpoint directly since streaming endpoints aren't supported
            response = session.post(
                API_URL, 
                headers=HEADERS, 
                json=payload, 
                timeout=REQUEST_TIMEOUT
            )
            
            response.raise_for_status()
            response_data = response.json()
            
            print(f"TTS_WORKER_DEBUG --- Response received: {type(response_data)}")
            
            # Process the complete JSON response
            token_counter = 0
            
            # Handle the response format: {"output": [{"output": {"generated_text": "<custom_token_4><custom_token_5>..."}}]}
            if isinstance(response_data, dict) and "output" in response_data:
                output_data = response_data["output"]
                
                if isinstance(output_data, list):
                    print(f"TTS_WORKER_DEBUG --- Processing {len(output_data)} items from output array")
                    
                    for item in output_data:
                        if isinstance(item, dict):
                            # Handle nested structure: item["output"]["generated_text"]
                            if "output" in item and isinstance(item["output"], dict):
                                generated_text = item["output"].get("generated_text", "")
                                print(f"TTS_WORKER_DEBUG --- Found generated_text with {len(generated_text)} characters")
                                
                                # Parse tokens from the generated text string
                                import re
                                # Find all custom tokens in the format <custom_token_XXXX>
                                token_pattern = r'<custom_token_\d+>'
                                tokens = re.findall(token_pattern, generated_text)
                                
                                print(f"TTS_WORKER_DEBUG --- Extracted {len(tokens)} custom tokens from generated text")
                                
                                for token_text in tokens:
                                    token_counter += 1
                                    perf_monitor.add_tokens()
                                    print(f"TTS_WORKER_DEBUG --- Yielding token {token_counter}: {token_text}")
                                    yield token_text
                                    
                            # Handle direct text format: item["text"]
                            elif "text" in item:
                                token_text = item["text"]
                                
                                # Check if this is a custom token
                                if token_text.startswith(CUSTOM_TOKEN_PREFIX) and token_text.endswith('>'):
                                    token_counter += 1
                                    perf_monitor.add_tokens()
                                    print(f"TTS_WORKER_DEBUG --- Yielding token {token_counter}: {token_text}")
                                    yield token_text
                                else:
                                    print(f"TTS_WORKER_DEBUG --- Skipping non-custom token: {token_text}")
                            else:
                                print(f"TTS_WORKER_DEBUG --- Item has unexpected structure: {list(item.keys())}")
                        else:
                            print(f"TTS_WORKER_DEBUG --- Non-dict item in output array: {type(item)}")
                else:
                    print(f"TTS_WORKER_DEBUG --- Unexpected output format: {type(output_data)}")
            else:
                print(f"TTS_WORKER_DEBUG --- No 'output' key in response or unexpected format")
                print(f"TTS_WORKER_DEBUG --- Response keys: {list(response_data.keys()) if isinstance(response_data, dict) else 'Not a dict'}")
            
            # Report completion
            generation_time = time.time() - start_time
            tokens_per_second = token_counter / generation_time if generation_time > 0 else 0
            print(f"Token processing complete: {token_counter} tokens in {generation_time:.2f}s ({tokens_per_second:.1f} tokens/sec)")
            
            if token_counter == 0:
                print("Warning: LLM response contained no custom tokens.")
                print(f"TTS_WORKER_DEBUG --- Full response for debugging: {json.dumps(response_data, indent=2)}")
            
            return  # Successful completion

        except requests.exceptions.HTTPError as http_err:
            print(f"HTTP error occurred: {http_err} - Status Code: {http_err.response.status_code}")
            print(f"Response text: {http_err.response.text}")
            if http_err.response.status_code >= 500:
                retry_count += 1
                if retry_count < max_retries:
                    wait_time = 2 ** retry_count
                    print(f"Retrying in {wait_time} seconds... (attempt {retry_count + 1}/{max_retries})")
                    time.sleep(wait_time)
                    continue
                else:
                    print("Max retries reached for HTTPError. Token generation failed.")
                    return
            else:
                print("Client-side HTTPError. Not retrying. Token generation failed.")
                return
            
        except requests.exceptions.Timeout:
            print(f"Request timed out after {REQUEST_TIMEOUT} seconds")
            retry_count += 1
            if retry_count < max_retries:
                wait_time = 2 ** retry_count
                print(f"Retrying in {wait_time} seconds... (attempt {retry_count+1}/{max_retries})")
                time.sleep(wait_time)
            else:
                print("Max retries reached for Timeout. Token generation failed.")
                return
                
        except requests.exceptions.ConnectionError as conn_err:
            print(f"Connection error to API at {API_URL}: {conn_err}")
            retry_count += 1
            if retry_count < max_retries:
                wait_time = 2 ** retry_count
                print(f"Retrying in {wait_time} seconds... (attempt {retry_count+1}/{max_retries})")
                time.sleep(wait_time)
            else:
                print("Max retries reached for ConnectionError. Token generation failed.")
                return
        
        except Exception as e: 
            print(f"An unexpected error occurred in generate_tokens_from_api: {type(e).__name__} - {e}")
            import traceback
            traceback.print_exc()
            print("Token generation failed due to an unexpected error.")
            return

    # Fallback if the while loop exits without a 'return' inside
    print("Token generation ultimately failed after all retries or due to a non-retryable error.")

# The turn_token_into_id function is now imported from speechpipe.py
# This eliminates duplicate code and ensures consistent behavior

def convert_to_audio(multiframe: List[int], count: int) -> Optional[bytes]:
    """Convert token frames to audio with performance monitoring."""
    # Import here to avoid circular imports
    from .speechpipe import convert_to_audio as orpheus_convert_to_audio
    start_time = time.time()
    result = orpheus_convert_to_audio(multiframe, count)
    
    if result is not None:
        perf_monitor.add_audio_chunk()
        
    return result

async def tokens_decoder(token_gen) -> AsyncGenerator[bytes, None]:
    """Simplified token decoder with early first-chunk processing for lower latency."""
    buffer = []
    count = 0
    
    # Use different thresholds for first chunk vs. subsequent chunks
    first_chunk_processed = False
    min_frames_first = 7  # Process after just 7 tokens for first chunk (ultra-low latency)
    min_frames_subsequent = 28  # Default for reliability after first chunk (4 chunks of 7)
    process_every = 7  # Process every 7 tokens (standard for Orpheus model)
    
    start_time = time.time()
    last_log_time = start_time
    token_count = 0
    
    async for token_text in token_gen:
        token = turn_token_into_id(token_text, count)
        if token is not None and token > 0:
            # Add to buffer using simple append (reliable method)
            buffer.append(token)
            count += 1
            token_count += 1
            
            # Log throughput periodically
            current_time = time.time()
            if current_time - last_log_time > 5.0:  # Every 5 seconds
                elapsed = current_time - start_time
                if elapsed > 0:
                    print(f"Token processing rate: {token_count/elapsed:.1f} tokens/second")
                last_log_time = current_time
            
            # Different processing paths based on whether first chunk has been processed
            if not first_chunk_processed:
                # For first audio output, process as soon as we have enough tokens for one chunk
                if count >= min_frames_first:
                    buffer_to_proc = buffer[-min_frames_first:]
                    
                    # Process the first chunk for immediate audio feedback
                    print(f"Processing first audio chunk with {len(buffer_to_proc)} tokens")
                    audio_samples = convert_to_audio(buffer_to_proc, count)
                    if audio_samples is not None:
                        first_chunk_processed = True  # Mark first chunk as processed
                        yield audio_samples
            else:
                # For subsequent chunks, use standard processing with larger batch
                if count % process_every == 0 and count >= min_frames_subsequent:
                    # Use simple slice operation - reliable and correct
                    buffer_to_proc = buffer[-min_frames_subsequent:]
                    
                    # Debug output to help diagnose issues
                    if count % 28 == 0:
                        print(f"Processing buffer with {len(buffer_to_proc)} tokens, total collected: {len(buffer)}")
                    
                    # Process the tokens
                    audio_samples = convert_to_audio(buffer_to_proc, count)
                    if audio_samples is not None:
                        yield audio_samples

def tokens_decoder_sync(syn_token_gen, output_file=None):
    """Optimized synchronous wrapper with parallel processing and efficient file I/O."""
    # Use a larger queue for high-end systems
    queue_size = 100 if HIGH_END_GPU else 50
    audio_queue = queue.Queue(maxsize=queue_size)
    audio_segments = []
    
    # If output_file is provided, prepare WAV file with buffered I/O
    wav_file = None
    if output_file:
        # Create directory if it doesn't exist
        os.makedirs(os.path.dirname(os.path.abspath(output_file)), exist_ok=True)
        wav_file = wave.open(output_file, "wb")
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(SAMPLE_RATE)
    
    # Batch processing of tokens for improved throughput
    batch_size = 32 if HIGH_END_GPU else 16
    
    # Thread synchronization for proper completion detection
    producer_done_event = threading.Event()
    producer_started_event = threading.Event()
    
    # Convert the synchronous token generator into an async generator with batching
    async def async_token_gen():
        batch = []
        for token in syn_token_gen:
            batch.append(token)
            if len(batch) >= batch_size:
                for t in batch:
                    yield t
                batch = []
        # Process any remaining tokens in the final batch
        for t in batch:
            yield t

    async def async_producer():
        # Track performance with more granular metrics
        start_time = time.time()
        chunk_count = 0
        last_log_time = start_time
        
        try:
            # Signal that producer has started processing
            producer_started_event.set()
            
            async for audio_chunk in tokens_decoder(async_token_gen()):
                # Process each audio chunk from the decoder
                if audio_chunk:
                    audio_queue.put(audio_chunk)
                    chunk_count += 1
                    
                    # Log performance periodically
                    current_time = time.time()
                    if current_time - last_log_time >= 3.0:  # Every 3 seconds
                        elapsed = current_time - last_log_time
                        if elapsed > 0:
                            recent_chunks = chunk_count
                            chunks_per_sec = recent_chunks / elapsed
                            print(f"Audio generation rate: {chunks_per_sec:.2f} chunks/second")
                        last_log_time = current_time
                        # Reset chunk counter for next interval
                        chunk_count = 0
        except Exception as e:
            print(f"Error in token processing: {str(e)}")
            import traceback
            traceback.print_exc()
        finally:
            # Always signal completion, even if there was an error
            print("Producer completed - setting done event")
            producer_done_event.set()
            # Add sentinel to queue to signal end of stream
            audio_queue.put(None)

    def run_async():
        """Run the async producer in its own thread"""
        asyncio.run(async_producer())

    # Use a separate thread with higher priority for producer
    thread = threading.Thread(target=run_async, name="TokenProcessor")
    thread.daemon = True  # Allow thread to be terminated when main thread exits
    thread.start()
    
    # Wait for producer to actually start before proceeding
    # This avoids race conditions where we might try to read from an empty queue
    # before the producer has had a chance to add anything
    producer_started_event.wait(timeout=5.0)
    
    # Optimized I/O approach for all systems
    # This approach is simpler and more reliable than separate code paths
    write_buffer = bytearray()
    buffer_max_size = 1024 * 1024  # 1MB max buffer size (adjustable)
    
    # Keep track of the last time we checked for completion
    last_check_time = time.time()
    check_interval = 1.0  # Check producer status every second
    
    # Process audio chunks until we're done
    while True:
        try:
            # Get the next audio chunk with a short timeout
            # This allows us to periodically check status and handle other events
            audio = audio_queue.get(timeout=0.1)
            
            # None marker indicates end of stream
            if audio is None:
                print("Received end-of-stream marker")
                break
            
            # Store the audio segment for return value
            audio_segments.append(audio)
            
            # Write to file if needed
            if wav_file:
                write_buffer.extend(audio)
                
                # Flush buffer if it's large enough
                if len(write_buffer) >= buffer_max_size:
                    wav_file.writeframes(write_buffer)
                    write_buffer = bytearray()  # Reset buffer
        
        except queue.Empty:
            # No data available right now
            current_time = time.time()
            
            # Periodically check if producer is done
            if current_time - last_check_time > check_interval:
                last_check_time = current_time
                
                # If producer is done and queue is empty, we're finished
                if producer_done_event.is_set() and audio_queue.empty():
                    print("Producer done and queue empty - finishing consumer")
                    break
                
                # Flush buffer periodically even if not full
                if wav_file and len(write_buffer) > 0:
                    wav_file.writeframes(write_buffer)
                    write_buffer = bytearray()  # Reset buffer
    
    # Extra safety check - ensure thread is done
    if thread.is_alive():
        print("Waiting for token processor thread to complete...")
        thread.join(timeout=10.0)
        if thread.is_alive():
            print("WARNING: Token processor thread did not complete within timeout")
    
    # Final flush of any remaining data
    if wav_file and len(write_buffer) > 0:
        print(f"Final buffer flush: {len(write_buffer)} bytes")
        wav_file.writeframes(write_buffer)
    
    # Close WAV file if opened
    if wav_file:
        wav_file.close()
        if output_file:
            print(f"Audio saved to {output_file}")
    
    # Calculate and print detailed performance metrics
    if audio_segments:
        total_bytes = sum(len(segment) for segment in audio_segments)
        duration = total_bytes / (2 * SAMPLE_RATE)  # 2 bytes per sample at 24kHz
        total_time = time.time() - perf_monitor.start_time
        realtime_factor = duration / total_time if total_time > 0 else 0
        
        print(f"Generated {len(audio_segments)} audio segments")
        print(f"Generated {duration:.2f} seconds of audio in {total_time:.2f} seconds")
        print(f"Realtime factor: {realtime_factor:.2f}x")
        
        if realtime_factor < 1.0:
            print("⚠️ Warning: Generation is slower than realtime")
        else:
            print(f"✓ Generation is {realtime_factor:.1f}x faster than realtime")
    
    return audio_segments

def stream_audio(audio_buffer):
    """Stream audio buffer to output device with error handling."""
    if audio_buffer is None or len(audio_buffer) == 0:
        return
    
    if not SOUNDDEVICE_AVAILABLE:
        print("Audio playback skipped: sounddevice not available")
        return
    
    try:
        # Convert bytes to NumPy array (16-bit PCM)
        audio_data = np.frombuffer(audio_buffer, dtype=np.int16)
        
        # Normalize to float in range [-1, 1] for playback
        audio_float = audio_data.astype(np.float32) / 32767.0
        
        # Play the audio with proper device selection and error handling
        sd.play(audio_float, SAMPLE_RATE)
        sd.wait()
    except Exception as e:
        print(f"Audio playback error: {e}")

import re
import numpy as np
from io import BytesIO
import wave

def split_text_into_sentences(text):
    """Split text into sentences with a more reliable approach."""
    # We'll use a simple approach that doesn't rely on variable-width lookbehinds
    # which aren't supported in Python's regex engine
    
    # First, split on common sentence ending punctuation
    # This isn't perfect but works for most cases and avoids the regex error
    parts = []
    current_sentence = ""
    
    for char in text:
        current_sentence += char
        
        # If we hit a sentence ending followed by a space, consider this a potential sentence end
        if char in (' ', '\n', '\t') and len(current_sentence) > 1:
            prev_char = current_sentence[-2]
            if prev_char in ('.', '!', '?'):
                # Check if this is likely a real sentence end and not an abbreviation
                # (Simple heuristic: if there's a space before the period, it's likely a real sentence end)
                if len(current_sentence) > 3 and current_sentence[-3] not in ('.', ' '):
                    parts.append(current_sentence.strip())
                    current_sentence = ""
    
    # Add any remaining text
    if current_sentence.strip():
        parts.append(current_sentence.strip())
    
    # Combine very short segments to avoid tiny audio files
    min_chars = 20  # Minimum reasonable sentence length
    combined_sentences = []
    i = 0
    
    while i < len(parts):
        current = parts[i]
        
        # If this is a short sentence and not the last one, combine with next
        while i < len(parts) - 1 and len(current) < min_chars:
            i += 1
            current += " " + parts[i]
            
        combined_sentences.append(current)
        i += 1
    
    return combined_sentences

def generate_speech_from_api(prompt, voice=DEFAULT_VOICE, output_file=None, temperature=TEMPERATURE, 
                     top_p=TOP_P, max_tokens=MAX_TOKENS, repetition_penalty=None, 
                             use_batching=True, max_batch_chars=2500, 
                             output_format: Optional[str] = "wav") -> Tuple[bool, Optional[str]]:
    """
    Generate speech from text using Orpheus model with performance optimizations.
    Returns a tuple: (success_status, error_message_or_none).
    """
    print(f"Starting speech generation for '{prompt[:50]}{'...' if len(prompt) > 50 else ''}'")
    print(f"Using voice: {voice}, Output Format: {output_format}, GPU acceleration: {'Yes (High-end)' if HIGH_END_GPU else 'Yes' if torch.cuda.is_available() else 'No'}")
    
    # Reset performance monitor
    global perf_monitor
    perf_monitor = PerformanceMonitor()
    
    start_time = time.time()
    
    try:
        all_audio_segments = []
        # For shorter text, use the standard non-batched approach
        if not use_batching or len(prompt) < max_batch_chars:
            all_audio_segments = tokens_decoder_sync(
                generate_tokens_from_api(
                    prompt=prompt, 
                    voice=voice,
                    temperature=temperature,
                    top_p=top_p,
                    max_tokens=max_tokens,
                    repetition_penalty=REPETITION_PENALTY  # Always use hardcoded value
                ),
                output_file=output_file
            )
        else:
            # For longer text, use sentence-based batching
            print(f"Using sentence-based batching for text with {len(prompt)} characters")
            sentences = split_text_into_sentences(prompt)
            print(f"Split text into {len(sentences)} segments")
            
            batches = []
            current_batch = ""
            for sentence in sentences:
                if len(current_batch) + len(sentence) > max_batch_chars and current_batch:
                    batches.append(current_batch)
                    current_batch = sentence
                else:
                    if current_batch:
                        current_batch += " "
                    current_batch += sentence
            if current_batch:
                batches.append(current_batch)
            
            print(f"Created {len(batches)} batches for processing")
            
            batch_temp_files = []
            for i, batch_text in enumerate(batches):
                print(f"Processing batch {i+1}/{len(batches)} ({len(batch_text)} characters)")
                temp_batch_output_file = None
                if output_file:
                    # Ensure 'outputs' directory exists for temp files if main output_file is specified
                    os.makedirs(os.path.dirname(os.path.abspath(output_file)), exist_ok=True)
                    temp_batch_output_file = f"{os.path.splitext(output_file)[0]}_temp_batch_{i}_{int(time.time())}.wav"
                    batch_temp_files.append(temp_batch_output_file)
                
                batch_segments_data = tokens_decoder_sync(
                    generate_tokens_from_api(
                        prompt=batch_text,
                        voice=voice,
                        temperature=temperature,
                        top_p=top_p,
                        max_tokens=max_tokens,
                        repetition_penalty=REPETITION_PENALTY
                    ),
                    output_file=temp_batch_output_file
                )
                all_audio_segments.extend(batch_segments_data)
            
            if output_file and batch_temp_files:
                stitch_wav_files(batch_temp_files, output_file)
                for temp_file in batch_temp_files:
                    try:
                        os.remove(temp_file)
                    except Exception as e:
                        print(f"Warning: Could not remove temporary file {temp_file}: {e}")
    
        # Report final performance metrics
        end_time = time.time()
        total_time = end_time - start_time
        
        if all_audio_segments:
            total_bytes_generated = sum(len(segment) for segment in all_audio_segments)
            duration_generated = total_bytes_generated / (2 * SAMPLE_RATE)
            print(f"Generated {len(all_audio_segments)} audio segments, total {duration_generated:.2f}s audio in {total_time:.2f}s.")
            if total_time > 0:
                print(f"Realtime factor: {duration_generated/total_time:.2f}x")
        else:
            print(f"No audio segments generated. Total time: {total_time:.2f} seconds")
            # If no audio segments were generated, it's a failure, regardless of file creation.
            if output_file and os.path.exists(output_file):
                # Log the empty file situation more clearly as an error before returning failure
                if os.path.getsize(output_file) <= 44: # Check for empty or header-only WAV
                    error_msg = f"Output file {output_file} was created but contains no audio data (size: {os.path.getsize(output_file)} bytes)."
                    print(f"ERROR: {error_msg}")
                    return False, error_msg
            
            error_msg = "No audio segments were generated during the process."
            print(f"ERROR: {error_msg}")
            return False, error_msg

        # Check if the output file was created and is not empty (beyond just a header)
        if output_file:
            if not os.path.exists(output_file):
                error_msg = f"Output file {output_file} was not created."
                print(f"ERROR: {error_msg}")
                return False, error_msg
            # A typical WAV header is 44 bytes. If it's that or less, it's effectively empty.
            if os.path.getsize(output_file) <= 44: 
                error_msg = f"Output file {output_file} is empty or contains only a header (size: {os.path.getsize(output_file)} bytes)."
                print(f"ERROR: {error_msg}")
                # Optionally remove empty file: os.remove(output_file)
                return False, error_msg
            print(f"Successfully generated speech to {output_file}")
        
        print(f"Total speech generation completed in {total_time:.2f} seconds")
        return True, None # Success

    except Exception as e:
        import traceback
        tb_str = traceback.format_exc()
        error_msg = f"""Error during speech generation: {str(e)}
Traceback:
{tb_str}"""
        print(error_msg)
        return False, str(e) # Return the error message

def stitch_wav_files(input_files, output_file, crossfade_ms=100):
    """Stitch multiple WAV files together with crossfading for smooth transitions."""
    if not input_files:
        return
        
    print(f"Stitching {len(input_files)} WAV files together with {crossfade_ms}ms crossfade")
    
    # If only one file, just copy it
    if len(input_files) == 1:
        import shutil
        shutil.copy(input_files[0], output_file)
        return
    
    # Convert crossfade_ms to samples
    crossfade_samples = int(SAMPLE_RATE * crossfade_ms / 1000)
    print(f"Using {crossfade_samples} samples for crossfade at {SAMPLE_RATE}Hz")
    
    # Build the final audio in memory with crossfades
    final_audio = np.array([], dtype=np.int16)
    first_params = None
    
    for i, input_file in enumerate(input_files):
        try:
            with wave.open(input_file, 'rb') as wav:
                if first_params is None:
                    first_params = wav.getparams()
                elif wav.getparams() != first_params:
                    print(f"Warning: WAV file {input_file} has different parameters")
                    
                frames = wav.readframes(wav.getnframes())
                audio = np.frombuffer(frames, dtype=np.int16)
                
                if i == 0:
                    # First segment - use as is
                    final_audio = audio
                else:
                    # Apply crossfade with previous segment
                    if len(final_audio) >= crossfade_samples and len(audio) >= crossfade_samples:
                        # Create crossfade weights
                        fade_out = np.linspace(1.0, 0.0, crossfade_samples)
                        fade_in = np.linspace(0.0, 1.0, crossfade_samples)
                        
                        # Apply crossfade
                        crossfade_region = (final_audio[-crossfade_samples:] * fade_out + 
                                           audio[:crossfade_samples] * fade_in).astype(np.int16)
                        
                        # Combine: original without last crossfade_samples + crossfade + new without first crossfade_samples
                        final_audio = np.concatenate([final_audio[:-crossfade_samples], 
                                                    crossfade_region, 
                                                    audio[crossfade_samples:]])
                    else:
                        # One segment too short for crossfade, just append
                        print(f"Segment {i} too short for crossfade, concatenating directly")
                        final_audio = np.concatenate([final_audio, audio])
        except Exception as e:
            print(f"Error processing file {input_file}: {e}")
            if i == 0:
                raise  # Critical failure if first file fails
    
    # Write the final audio data to the output file
    try:
        with wave.open(output_file, 'wb') as output_wav:
            if first_params is None:
                raise ValueError("No valid WAV files were processed")
                
            output_wav.setparams(first_params)
            output_wav.writeframes(final_audio.tobytes())
        
        print(f"Successfully stitched audio to {output_file} with crossfading")
    except Exception as e:
        print(f"Error writing output file {output_file}: {e}")
        raise

def list_available_voices():
    """List all available voices with the recommended one marked."""
    print("Available voices (in order of conversational realism):")
    for i, voice in enumerate(AVAILABLE_VOICES):
        marker = "★" if voice == DEFAULT_VOICE else " "
        print(f"{marker} {voice}")
    print(f"\nDefault voice: {DEFAULT_VOICE}")
    
    print("\nAvailable emotion tags:")
    print("<laugh>, <chuckle>, <sigh>, <cough>, <sniffle>, <groan>, <yawn>, <gasp>")

def main():
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="Orpheus Text-to-Speech using Orpheus-FASTAPI")
    parser.add_argument("--text", type=str, help="Text to convert to speech")
    parser.add_argument("--voice", type=str, default=DEFAULT_VOICE, help=f"Voice to use (default: {DEFAULT_VOICE})")
    parser.add_argument("--output", type=str, help="Output WAV file path")
    parser.add_argument("--list-voices", action="store_true", help="List available voices")
    parser.add_argument("--temperature", type=float, default=TEMPERATURE, help="Temperature for generation")
    parser.add_argument("--top_p", type=float, default=TOP_P, help="Top-p sampling parameter")
    parser.add_argument("--repetition_penalty", type=float, default=REPETITION_PENALTY, 
                       help="Repetition penalty (fixed at 1.1 for stable generation - parameter kept for compatibility)")
    
    args = parser.parse_args()
    
    if args.list_voices:
        list_available_voices()
        return
    
    # Use text from command line or prompt user
    prompt = args.text
    if not prompt:
        if len(sys.argv) > 1 and sys.argv[1] not in ("--voice", "--output", "--temperature", "--top_p", "--repetition_penalty"):
            prompt = " ".join([arg for arg in sys.argv[1:] if not arg.startswith("--")])
        else:
            prompt = input("Enter text to synthesize: ")
            if not prompt:
                prompt = "Hello, I am Orpheus, an AI assistant with emotional speech capabilities."
    
    # Default output file if none provided
    output_file = args.output
    if not output_file:
        # Create outputs directory if it doesn't exist
        os.makedirs("outputs", exist_ok=True)
        # Generate a filename based on the voice and a timestamp
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        output_file = f"outputs/{args.voice}_{timestamp}.wav"
        print(f"No output file specified. Saving to {output_file}")
    
    # Generate speech
    start_time = time.time()
    success, error_msg = generate_speech_from_api(
        prompt=prompt,
        voice=args.voice,
        temperature=args.temperature,
        top_p=args.top_p,
        repetition_penalty=args.repetition_penalty,
        output_file=output_file
    )
    end_time = time.time()
    
    print(f"Speech generation completed in {end_time - start_time:.2f} seconds")
    if success:
        print(f"Audio saved to {output_file}")
    else:
        print(f"Speech generation failed. Error: {error_msg}")

if __name__ == "__main__":
    main()
