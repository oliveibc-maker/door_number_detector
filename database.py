"""
Gerenciador de Banco de Dados
Suporta múltiplos tipos: Oracle, PostgreSQL, MySQL, SQLite
"""

import logging
import sqlalchemy
from sqlalchemy import create_engine, Column, String, Float, Integer, DateTime, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime

logger = logging.getLogger(__name__)
Base = declarative_base()


class DoorDetectionResult(Base):
    """Modelo ORM para resultados de detecção"""
    __tablename__ = 'door_detection_results'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    door_number = Column(String(50))
    confidence = Column(Float)
    heading = Column(Integer)
    pitch = Column(Integer)
    timestamp = Column(DateTime, default=datetime.now)
    raw_ocr_text = Column(Text)
    status = Column(String(20), default='success')
    error_message = Column(Text)


class DatabaseManager:
    """Gerencia operações com banco de dados"""
    
    def __init__(self, config):
        self.config = config
        self.engine = self._create_engine()
        self.Session = sessionmaker(bind=self.engine)
        self._init_db()
    
    def _create_engine(self):
        """Cria engine SQLAlchemy baseado na configuração"""
        db_type = self.config.db_type
        
        try:
            if db_type == 'oracle':
                connection_string = self._oracle_connection_string()
            elif db_type == 'postgresql':
                connection_string = self._postgresql_connection_string()
            elif db_type == 'mysql':
                connection_string = self._mysql_connection_string()
            elif db_type == 'sqlite':
                connection_string = 'sqlite:///door_detector.db'
            else:
                raise ValueError(f"Tipo de banco desconhecido: {db_type}")
            
            logger.info(f"Conectando ao banco {db_type}...")
            engine = create_engine(connection_string, echo=self.config.debug)
            return engine
            
        except Exception as e:
            logger.error(f"Erro ao criar engine: {str(e)}")
            raise
    
    def _oracle_connection_string(self):
        """Constrói string de conexão Oracle"""
        user = self.config.db_user
        password = self.config.db_password
        host = self.config.db_host
        port = self.config.db_port
        sid = self.config.db_sid
        
        # Formato: oracle+cx_Oracle://user:password@host:port/?service_name=sid
        return f"oracle+cx_Oracle://{user}:{password}@{host}:{port}/?service_name={sid}"
    
    def _postgresql_connection_string(self):
        """Constrói string de conexão PostgreSQL"""
        user = self.config.db_user
        password = self.config.db_password
        host = self.config.db_host
        port = self.config.db_port
        database = self.config.db_name
        
        return f"postgresql://{user}:{password}@{host}:{port}/{database}"
    
    def _mysql_connection_string(self):
        """Constrói string de conexão MySQL"""
        user = self.config.db_user
        password = self.config.db_password
        host = self.config.db_host
        port = self.config.db_port
        database = self.config.db_name
        
        return f"mysql+pymysql://{user}:{password}@{host}:{port}/{database}"
    
    def _init_db(self):
        """Inicializa tabelas no banco de dados"""
        try:
            Base.metadata.create_all(self.engine)
            logger.info("Tabelas criadas/verificadas com sucesso")
        except Exception as e:
            logger.error(f"Erro ao criar tabelas: {str(e)}")
            raise
    
    def save_result(self, result):
        """
        Salva resultado da detecção no banco de dados
        
        Args:
            result (dict): Resultado da detecção
        """
        session = self.Session()
        try:
            detection = DoorDetectionResult(
                latitude=result['latitude'],
                longitude=result['longitude'],
                door_number=result.get('door_number'),
                confidence=result.get('confidence', 0),
                heading=result.get('heading'),
                pitch=result.get('pitch'),
                timestamp=datetime.now(),
                status='success' if result['success'] else 'error',
                error_message=result.get('error')
            )
            
            session.add(detection)
            session.commit()
            logger.info(f"Resultado salvo com ID: {detection.id}")
            
        except Exception as e:
            session.rollback()
            logger.error(f"Erro ao salvar resultado: {str(e)}")
            raise
        finally:
            session.close()
    
    def get_results(self, limit=100):
        """
        Recupera últimos resultados
        
        Args:
            limit (int): Número máximo de registros
        
        Returns:
            list: Lista de resultados
        """
        session = self.Session()
        try:
            results = session.query(DoorDetectionResult)\
                .order_by(DoorDetectionResult.timestamp.desc())\
                .limit(limit)\
                .all()
            
            return [self._to_dict(r) for r in results]
            
        finally:
            session.close()
    
    def _to_dict(self, obj):
        """Converte objeto ORM para dicionário"""
        return {
            'id': obj.id,
            'latitude': obj.latitude,
            'longitude': obj.longitude,
            'door_number': obj.door_number,
            'confidence': obj.confidence,
            'timestamp': obj.timestamp.isoformat(),
            'status': obj.status
        }
    
    def close(self):
        """Fecha conexão com banco"""
        self.engine.dispose()
        logger.info("Conexão com banco fechada")