import llm
import os
import json
import time
import httpx
from datetime import datetime
from typing import Optional, Dict, Any, List, Generator
from pydantic import Field, field_validator


@llm.hookimpl
def register_models(register):
    register(GitHubCopilot())


class GitHubCopilotAuthenticator:
    """
    Handles authentication with GitHub Copilot using device code flow.
    """
    def __init__(self) -> None:
        # Constants for GitHub API
        self.github_client_id = "Iv1.b507a08c87ecfe98"  # GitHub Copilot client ID
        self.github_device_code_url = "https://github.com/login/device/code"
        self.github_access_token_url = "https://github.com/login/oauth/access_token"
        self.github_api_key_url = "https://api.github.com/copilot_internal/v2/token"
        
        # Token storage paths
        self.token_dir = os.getenv(
            "GITHUB_COPILOT_TOKEN_DIR",
            os.path.expanduser("~/.config/llm/github_copilot")
        )
        self.access_token_file = os.path.join(
            self.token_dir,
            os.getenv("GITHUB_COPILOT_ACCESS_TOKEN_FILE", "access-token")
        )
        self.api_key_file = os.path.join(
            self.token_dir, 
            os.getenv("GITHUB_COPILOT_API_KEY_FILE", "api-key.json")
        )
        self._ensure_token_dir()

    def _ensure_token_dir(self) -> None:
        """Ensure the token directory exists."""
        if not os.path.exists(self.token_dir):
            os.makedirs(self.token_dir, exist_ok=True)

    def _get_github_headers(self, access_token: Optional[str] = None) -> Dict[str, str]:
        """Generate standard GitHub headers for API requests."""
        headers = {
            "accept": "application/json",
            "editor-version": "vscode/1.85.1",
            "editor-plugin-version": "copilot/1.155.0",
            "user-agent": "GithubCopilot/1.155.0",
            "accept-encoding": "gzip,deflate,br",
        }
        
        if access_token:
            headers["authorization"] = f"token {access_token}"
            
        if "content-type" not in headers:
            headers["content-type"] = "application/json"
            
        return headers

    def get_access_token(self) -> str:
        """
        Get GitHub access token, refreshing if necessary.
        """
        try:
            with open(self.access_token_file, "r") as f:
                access_token = f.read().strip()
                if access_token:
                    return access_token
        except IOError:
            pass

        # No valid token found, need to login
        for attempt in range(3):
            try:
                access_token = self._login()
                try:
                    with open(self.access_token_file, "w") as f:
                        f.write(access_token)
                except IOError:
                    print("Error saving access token to file")
                return access_token
            except Exception as e:
                print(f"Login attempt {attempt + 1} failed: {str(e)}")
                if attempt == 2:  # Last attempt
                    raise Exception("Failed to get access token after 3 attempts")
                continue

    def get_api_key(self) -> str:
        """
        Get the API key, refreshing if necessary.
        """
        try:
            with open(self.api_key_file, "r") as f:
                api_key_info = json.load(f)
                if api_key_info.get("expires_at", 0) > datetime.now().timestamp():
                    return api_key_info.get("token")
                else:
                    print("API key expired, refreshing")
        except (IOError, json.JSONDecodeError, KeyError):
            pass

        try:
            api_key_info = self._refresh_api_key()
            with open(self.api_key_file, "w") as f:
                json.dump(api_key_info, f)
            return api_key_info.get("token")
        except Exception as e:
            raise Exception(f"Failed to get API key: {str(e)}")

    def _get_device_code(self) -> Dict[str, str]:
        """
        Get a device code for GitHub authentication.
        """
        try:
            client = httpx.Client()
            resp = client.post(
                self.github_device_code_url,
                headers=self._get_github_headers(),
                json={"client_id": self.github_client_id, "scope": "read:user"},
            )
            resp.raise_for_status()
            resp_json = resp.json()

            required_fields = ["device_code", "user_code", "verification_uri"]
            if not all(field in resp_json for field in required_fields):
                raise Exception("Response missing required fields")
                
            return resp_json
        except Exception as e:
            raise Exception(f"Failed to get device code: {str(e)}")

    def _poll_for_access_token(self, device_code: str) -> str:
        """
        Poll for an access token after user authentication.
        """
        client = httpx.Client()
        max_attempts = 12  # 1 minute (12 * 5 seconds)
        
        for attempt in range(max_attempts):
            try:
                resp = client.post(
                    self.github_access_token_url,
                    headers=self._get_github_headers(),
                    json={
                        "client_id": self.github_client_id,
                        "device_code": device_code,
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    },
                )
                resp.raise_for_status()
                resp_json = resp.json()

                if "access_token" in resp_json:
                    print("Authentication successful!")
                    return resp_json["access_token"]
                elif "error" in resp_json and resp_json.get("error") == "authorization_pending":
                    print(f"Waiting for authorization... (attempt {attempt+1}/{max_attempts})")
                else:
                    print(f"Unexpected response: {resp_json}")
            except Exception as e:
                raise Exception(f"Failed to get access token: {str(e)}")
                
            time.sleep(5)
            
        raise Exception("Timed out waiting for user to authorize the device")

    def _login(self) -> str:
        """
        Login to GitHub Copilot using device code flow.
        """
        device_code_info = self._get_device_code()
        
        device_code = device_code_info["device_code"]
        user_code = device_code_info["user_code"]
        verification_uri = device_code_info["verification_uri"]

        print(
            f"\nPlease visit {verification_uri} and enter code {user_code} to authenticate GitHub Copilot.\n"
        )
        
        return self._poll_for_access_token(device_code)

    def _refresh_api_key(self) -> Dict[str, Any]:
        """
        Refresh the API key using the access token.
        """
        access_token = self.get_access_token()
        headers = self._get_github_headers(access_token)

        max_retries = 3
        for attempt in range(max_retries):
            try:
                client = httpx.Client()
                response = client.get(
                    self.github_api_key_url, 
                    headers=headers
                )
                response.raise_for_status()

                response_json = response.json()

                if "token" in response_json:
                    return response_json
                else:
                    print(f"API key response missing token: {response_json}")
            except Exception as e:
                print(f"Error refreshing API key (attempt {attempt+1}/{max_retries}): {str(e)}")

            if attempt < max_retries - 1:
                time.sleep(1)

        raise Exception("Failed to refresh API key after maximum retries")


class GitHubCopilot(llm.Model):
    """
    GitHub Copilot model implementation for LLM.
    """
    model_id = "github-copilot"
    can_stream = True
    
    # Map of model names to API model identifiers
    MODEL_MAPPINGS = {
        "github-copilot": "gpt-4o",
        "github-copilot/claude-3-7-sonnet-thought": "claude-3-7-sonnet-thought",
    }
    
    class Options(llm.Options):
        """
        Options for the GitHub Copilot model.
        """
        max_tokens: Optional[int] = Field(
            description="Maximum number of tokens to generate",
            default=1024
        )
        temperature: Optional[float] = Field(
            description="Controls randomness in the output",
            default=0.7
        )
        
        @field_validator("max_tokens")
        def validate_max_tokens(cls, max_tokens):
            if max_tokens is None:
                return None
            if max_tokens < 1:
                raise ValueError("max_tokens must be >= 1")
            return max_tokens
            
        @field_validator("temperature")
        def validate_temperature(cls, temperature):
            if temperature is None:
                return None
            if not 0 <= temperature <= 1:
                raise ValueError("temperature must be between 0 and 1")
            return temperature
        
    def __init__(self):
        self.authenticator = GitHubCopilotAuthenticator()
        # GitHub Copilot API base URL - alternative URL format
        self.api_base = "https://api.githubcopilot.com"
        
    def _get_model_for_api(self, model: str) -> str:
        """Convert model name to API-compatible format."""
        # Strip provider prefix if present
        if '/' in model:
            _, model_name = model.split('/', 1)
            if model_name in self.MODEL_MAPPINGS.values():
                return model_name
        
        # Use the mapping or default to gpt-4o
        return self.MODEL_MAPPINGS.get(model, "gpt-4o")
        
    def execute(self, prompt, stream, response, conversation):
        """
        Execute the GitHub Copilot completion.
        """
        # Get API key
        try:
            api_key = self.authenticator.get_api_key()
        except Exception as e:
            yield f"Error getting GitHub Copilot API key: {str(e)}"
            return
        
        # Get model name
        model_name = self._get_model_for_api(self.model_id)
        
        # Prepare the request with required headers
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "editor-version": "vscode/1.85.1",
            "editor-plugin-version": "copilot/1.155.0",
            "user-agent": "GithubCopilot/1.155.0",
            "Copilot-Integration-Id": "vscode-chat",  # Use a recognized integration ID
        }
        
        # Print debugging information about the API key
        print(f"Debug: API key starts with {api_key[:4]}...")
        print(f"Debug: API key length: {len(api_key)}")
        print(f"Debug: Using auth header: Bearer {api_key[:4]}...")
        
        # Extract messages from conversation
        messages = []
        if conversation and conversation.responses:
            for prev_response in conversation.responses:
                # Add user message
                messages.append({
                    "role": "user",
                    "content": prev_response.prompt.prompt
                })
                # Add assistant message
                messages.append({
                    "role": "assistant",
                    "content": prev_response.text()
                })
                
        # Add the current prompt
        if messages:
            # Add system message if not present
            if not any(msg.get("role") == "system" for msg in messages):
                messages.insert(0, {
                    "role": "system",
                    "content": "You are GitHub Copilot, an AI programming assistant."
                })
            # Add the current prompt
            messages.append({
                "role": "user",
                "content": prompt.prompt
            })
        else:
            # First message in conversation
            messages = [
                {
                    "role": "system",
                    "content": "You are GitHub Copilot, an AI programming assistant."
                },
                {
                    "role": "user",
                    "content": prompt.prompt
                }
            ]
            
        # Get options
        max_tokens = prompt.options.max_tokens or 1024
        temperature = prompt.options.temperature or 0.7
        
        # Prepare payload
        payload = {
            "model": model_name,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": stream,
        }
        
        # Record additional information in response_json
        response.response_json = {
            "model": model_name,
            "usage": {
                "prompt_tokens": 0,  # Will be updated in non-streaming responses
                "completion_tokens": 0,  # Will be updated in non-streaming responses
                "total_tokens": 0  # Will be updated in non-streaming responses
            }
        }
        
        client = httpx.Client()
        print(f"Debug: Using API endpoint {self.api_base}/chat/completions")
        print(f"Debug: Using model {model_name}")
        
        # Make a simple diagnostic request first
        try:
            diagnostic_response = client.post(
                f"{self.api_base}/chat/completions",
                headers=headers,
                json={**payload, "stream": False},  # Force non-streaming for diagnostics
                timeout=120
            )
            print(f"Debug: Diagnostic response status: {diagnostic_response.status_code}")
            print(f"Debug: Diagnostic content type: {diagnostic_response.headers.get('content-type', 'none')}")
            print(f"Debug: Diagnostic response length: {len(diagnostic_response.content)}")
            
            try:
                text_content = diagnostic_response.text
                if text_content:
                    print(f"Debug: Response text: {text_content[:100]}...")
                    yield text_content
                    return  # We got a response, return it
            except Exception as text_err:
                print(f"Debug: Couldn't access text: {str(text_err)}")
        
        except Exception as diag_err:
            print(f"Debug: Diagnostic request failed: {str(diag_err)}")
            # Continue to try the streaming request
        
        # Try the streaming request
        try:
            with client.stream(
                "POST",
                f"{self.api_base}/chat/completions",
                headers=headers,
                json=payload,
                timeout=120
            ) as stream_response:
                print(f"Debug: Stream response status: {stream_response.status_code}")
                
                # Process the streaming response
                for line in stream_response.iter_lines():
                    if not line:
                        continue
                    
                    # Debug the line
                    line_str = line.decode('utf-8') if isinstance(line, bytes) else line
                    print(f"Debug: Stream line: {line_str[:50]}...")
                    
                    # Parse SSE format
                    if line_str.startswith("data: "):
                        data = line_str[6:]
                        if data == "[DONE]":
                            break
                        
                        try:
                            # Try to parse as JSON
                            json_data = json.loads(data)
                            content = None
                            if "choices" in json_data and json_data["choices"]:
                                delta = json_data["choices"][0].get("delta", {})
                                content = delta.get("content")
                            
                            if content:
                                yield content
                        except json.JSONDecodeError:
                            # Not JSON, yield raw
                            yield data
                    else:
                        # Not SSE format, yield as is
                        yield line_str
        
        except httpx.HTTPStatusError as e:
            try:
                error_detail = e.response.text if hasattr(e, 'response') and e.response else "No response text available"
                status_code = e.response.status_code if hasattr(e, 'response') and e.response else "unknown"
                error_message = f"HTTP error: {str(e)}\nStatus code: {status_code}\nDetails: {error_detail}"
                print(f"Debug: HTTP error - Status: {status_code}")
                print(f"Debug: HTTP error - Response: {error_detail}")
            except Exception as inner_e:
                error_message = f"HTTP error: {str(e)}\nCould not get details: {str(inner_e)}"
                print(f"Debug: Failed to extract error details: {str(inner_e)}")
            yield error_message
            return
                
        except Exception as e:
            error_message = f"Error with GitHub Copilot request: {str(e)}"
            print(f"Debug: General exception: {type(e).__name__}")
            print(f"Debug: {error_message}")
            yield error_message
