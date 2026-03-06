"""
Door Number Detector - Main Script
Valida números de portas através do Google Street View usando OCR (Tesseract)
Coordenadas: 40.80082, -8.593741
"""

import os
import sys
import logging
import pytesseract
import requests
from PIL import Image
from io import BytesIO
from datetime import datetime
from database import DatabaseManager
from config import Config
from google_street_view import StreetViewFetcher

# Configurar logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('door_detector.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class DoorNumberDetector:
    """
    Detecta números de portas em coordenadas usando Google Street View e OCR
    """
    
    def __init__(self, config_path='config.ini'):
        """Inicializa o detector com configurações"""
        self.config = Config(config_path)
        self.db = DatabaseManager(self.config)
        self.street_view = StreetViewFetcher(self.config.google_api_key)
        logger.info("Door Number Detector inicializado")
    
    def detect_door_number(self, latitude, longitude, heading=0, pitch=0):
        """
        Detecta o número da porta em uma coordenada específica
        
        Args:
            latitude (float): Latitude da coordenada
            longitude (float): Longitude da coordenada
            heading (int): Direção da câmera (0-360)
            pitch (int): Ângulo vertical da câmera (-90 a 90)
        
        Returns:
            dict: Resultado da detecção com número da porta e confiança
        """
        logger.info(f"Processando coordenada: {latitude}, {longitude}")
        
        try:
            # Obter imagem do Street View
            image = self.street_view.get_image(latitude, longitude, heading, pitch)
            
            if image is None:
                logger.warning(f"Não foi possível obter imagem para {latitude}, {longitude}")
                return {
                    'success': False,
                    'latitude': latitude,
                    'longitude': longitude,
                    'door_number': None,
                    'confidence': 0,
                    'error': 'Imagem não disponível'
                }
            
            # Executar OCR na imagem
            door_number, confidence = self._extract_door_number(image)
            
            result = {
                'success': True,
                'latitude': latitude,
                'longitude': longitude,
                'door_number': door_number,
                'confidence': confidence,
                'heading': heading,
                'pitch': pitch,
                'timestamp': datetime.now().isoformat()
            }
            
            logger.info(f"Resultado: {door_number} (confiança: {confidence}%)")
            
            # Salvar resultado no banco de dados
            self.db.save_result(result)
            
            return result
            
        except Exception as e:
            logger.error(f"Erro ao processar coordenada: {str(e)}", exc_info=True)
            return {
                'success': False,
                'latitude': latitude,
                'longitude': longitude,
                'error': str(e)
            }
    
    def _extract_door_number(self, image):
        """
        Extrai número da porta usando OCR Tesseract
        
        Args:
            image (PIL.Image): Imagem da Street View
        
        Returns:
            tuple: (número_da_porta, confiança_percentual)
        """
        try:
            # Converter para grayscala para melhor OCR
            gray_image = image.convert('L')
            
            # Executar Tesseract OCR
            text = pytesseract.image_to_string(gray_image)
            
            # Processar resultado
            door_number = self._parse_door_number(text)
            
            # Calcular confiança (simplificado)
            confidence = 85 if door_number else 0
            
            return door_number, confidence
            
        except Exception as e:
            logger.error(f"Erro no OCR: {str(e)}")
            return None, 0
    
    def _parse_door_number(self, text):
        """
        Extrai número da porta do texto OCR
        
        Args:
            text (str): Texto extraído pelo OCR
        
        Returns:
            str: Número da porta ou None
        """
        if not text:
            return None
        
        # Procurar por números
        import re
        numbers = re.findall(r'\b\d+[A-Z]?\b', text)
        
        if numbers:
            return numbers[0]  # Retornar primeiro número encontrado
        
        return None
    
    def process_coordinates_batch(self, coordinates_list):
        """
        Processa múltiplas coordenadas
        
        Args:
            coordinates_list (list): Lista de dicts com 'latitude' e 'longitude'
        
        Returns:
            list: Lista de resultados
        """
        results = []
        total = len(coordinates_list)
        
        for idx, coord in enumerate(coordinates_list, 1):
            logger.info(f"Processando {idx}/{total}")
            result = self.detect_door_number(
                coord['latitude'],
                coord['longitude'],
                coord.get('heading', 0),
                coord.get('pitch', 0)
            )
            results.append(result)
        
        return results
    
    def close(self):
        """Fecha conexões e recursos"""
        self.db.close()
        logger.info("Door Number Detector finalizado")


def main():
    """Função principal"""
    try:
        detector = DoorNumberDetector()
        
        # Processar coordenada de exemplo
        result = detector.detect_door_number(40.80082, -8.593741)
        
        print("\n" + "="*50)
        print("RESULTADO DA DETECÇÃO")
        print("="*50)
        print(f"Latitude: {result['latitude']}")
        print(f"Longitude: {result['longitude']}")
        print(f"Número da Porta: {result.get('door_number', 'N/A')}")
        print(f"Confiança: {result.get('confidence', 0)}%")
        print(f"Status: {'✓ Sucesso' if result['success'] else '✗ Erro'}")
        if not result['success']:
            print(f"Erro: {result.get('error', 'Desconhecido')}")
        print("="*50)
        
        detector.close()
        
    except Exception as e:
        logger.error(f"Erro fatal: {str(e)}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()