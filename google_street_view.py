"""
Integração com Google Street View API
"""

import logging
import requests
from PIL import Image
from io import BytesIO

logger = logging.getLogger(__name__)


class StreetViewFetcher:
    """Obtém imagens do Google Street View"""
    
    BASE_URL = "https://maps.googleapis.com/maps/api/streetview"
    
    def __init__(self, api_key, size='640x480'):
        """
        Inicializa o fetcher
        
        Args:
            api_key (str): Chave da API do Google
            size (str): Tamanho da imagem (WIDTHxHEIGHT)
        """
        self.api_key = api_key
        self.size = size
    
    def get_image(self, latitude, longitude, heading=0, pitch=0, fov=90):
        """
        Obtém imagem do Street View
        
        Args:
            latitude (float): Latitude
            longitude (float): Longitude
            heading (int): Direção (0-360)
            pitch (int): Ângulo vertical (-90 a 90)
            fov (int): Campo de visão (10-120)
        
        Returns:
            PIL.Image: Imagem obtida ou None em caso de erro
        """
        try:
            params = {
                'location': f'{latitude},{longitude}',
                'size': self.size,
                'heading': heading,
                'pitch': pitch,
                'fov': fov,
                'key': self.api_key
            }
            
            logger.info(f"Obtendo imagem para: {latitude}, {longitude}")
            
            response = requests.get(self.BASE_URL, params=params, timeout=10)
            response.raise_for_status()
            
            # Verificar se é uma imagem válida
            if response.headers['content-type'].startswith('image'):
                image = Image.open(BytesIO(response.content))
                logger.info("Imagem obtida com sucesso")
                return image
            else:
                logger.warning(f"Resposta não é uma imagem: {response.headers['content-type']}")
                return None
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Erro ao obter imagem: {str(e)}")
            return None
        except Exception as e:
            logger.error(f"Erro inesperado: {str(e)}")
            return None
    
    def is_imagery_available(self, latitude, longitude):
        """
        Verifica se há imagem disponível para uma coordenada
        
        Args:
            latitude (float): Latitude
            longitude (float): Longitude
        
        Returns:
            bool: True se há imagem disponível
        """
        params = {
            'location': f'{latitude},{longitude}',
            'key': self.api_key
        }
        
        try:
            response = requests.head(
                f"{self.BASE_URL}",
                params=params,
                timeout=5
            )
            return response.status_code == 200
        except:
            return False