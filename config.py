"""
Configurações do Projeto
"""

import configparser
import os
from pathlib import Path


class Config:
    """Gerencia configurações da aplicação"""
    
    def __init__(self, config_file='config.ini'):
        self.config = configparser.ConfigParser()
        self.config_file = config_file
        
        if os.path.exists(config_file):
            self.config.read(config_file)
        else:
            self._create_default_config()
    
    def _create_default_config(self):
        """Cria arquivo de configuração padrão"""
        self.config['GOOGLE'] = {
            'api_key': os.getenv('GOOGLE_API_KEY', 'YOUR_API_KEY_HERE'),
            'street_view_size': '640x480'
        }
        
        self.config['DATABASE'] = {
            'type': 'oracle',  # oracle, postgresql, mysql, sqlite
            'host': os.getenv('DB_HOST', 'localhost'),
            'port': os.getenv('DB_PORT', '1521'),
            'user': os.getenv('DB_USER', 'system'),
            'password': os.getenv('DB_PASSWORD', ''),
            'database': os.getenv('DB_NAME', 'xe'),
            'sid': os.getenv('DB_SID', 'xe')
        }
        
        self.config['OCR'] = {
            'tesseract_path': os.getenv('TESSERACT_PATH', '/usr/bin/tesseract'),
            'language': 'por+eng',  # Português + English
            'confidence_threshold': '70'
        }
        
        self.config['APP'] = {
            'debug': 'False',
            'log_level': 'INFO',
            'max_retries': '3'
        }
        
        self._save_config()
    
    def _save_config(self):
        """Salva configurações em arquivo"""
        with open(self.config_file, 'w') as f:
            self.config.write(f)
    
    @property
    def google_api_key(self):
        return self.config.get('GOOGLE', 'api_key')
    
    @property
    def street_view_size(self):
        return self.config.get('GOOGLE', 'street_view_size')
    
    @property
    def db_type(self):
        return self.config.get('DATABASE', 'type').lower()
    
    @property
    def db_host(self):
        return self.config.get('DATABASE', 'host')
    
    @property
    def db_port(self):
        return self.config.getint('DATABASE', 'port')
    
    @property
    def db_user(self):
        return self.config.get('DATABASE', 'user')
    
    @property
    def db_password(self):
        return self.config.get('DATABASE', 'password')
    
    @property
    def db_name(self):
        return self.config.get('DATABASE', 'database')
    
    @property
    def db_sid(self):
        return self.config.get('DATABASE', 'sid')
    
    @property
    def tesseract_path(self):
        return self.config.get('OCR', 'tesseract_path')
    
    @property
    def ocr_language(self):
        return self.config.get('OCR', 'language')
    
    @property
    def debug(self):
        return self.config.getboolean('APP', 'debug')