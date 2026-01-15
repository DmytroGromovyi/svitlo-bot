import os
import json
import base64
import requests
import logging

logger = logging.getLogger(__name__)

class GitHubSync:
    def __init__(self):
        self.token = os.getenv('GITHUB_TOKEN')
        self.repo = os.getenv('GITHUB_REPO')  # e.g., "DmytroGromovyi/svitlo-bot"
        self.branch = os.getenv('GITHUB_BRANCH', 'main')
        self.enabled = bool(self.token and self.repo)
        
        if not self.enabled:
            logger.warning("GitHub sync disabled - GITHUB_TOKEN or GITHUB_REPO not set")
    
    def sync_file(self, filepath, content):
        """Sync file content to GitHub repository"""
        if not self.enabled:
            return False
        
        try:
            # GitHub API endpoint
            url = f"https://api.github.com/repos/{self.repo}/contents/{filepath}"
            
            headers = {
                'Authorization': f'token {self.token}',
                'Accept': 'application/vnd.github.v3+json'
            }
            
            # Get current file SHA (needed for update)
            response = requests.get(url, headers=headers, params={'ref': self.branch})
            
            sha = None
            if response.status_code == 200:
                sha = response.json()['sha']
            
            # Encode content as base64
            content_bytes = content.encode('utf-8')
            content_base64 = base64.b64encode(content_bytes).decode('utf-8')
            
            # Update or create file
            data = {
                'message': f'Update {filepath} from bot',
                'content': content_base64,
                'branch': self.branch
            }
            
            if sha:
                data['sha'] = sha
            
            response = requests.put(url, headers=headers, json=data)
            response.raise_for_status()
            
            logger.info(f"✓ Synced {filepath} to GitHub")
            return True
            
        except Exception as e:
            logger.error(f"Failed to sync to GitHub: {e}")
            return False
    
    def sync_users(self, users_dict):
        """Sync users.json to GitHub"""
        content = json.dumps(users_dict, ensure_ascii=False, indent=2)
        return self.sync_file('data/users.json', content)