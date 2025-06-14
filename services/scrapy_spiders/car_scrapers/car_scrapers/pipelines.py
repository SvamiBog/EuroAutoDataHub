# services/scrapy_spiders/car_scrapers/car_scrapers/pipelines.py
import json
import logging
from itemadapter import ItemAdapter
from kafka import KafkaProducer
from kafka.errors import KafkaError
from .items import ParsedAdItem, ActiveIdsItem # Added MakeItem, ModelItem


class KafkaPipeline:
    def __init__(self, kafka_bootstrap_servers, kafka_topic_ads, kafka_topic_active_ids, kafka_producer_config):
        self.producer = None
        self.logger = logging.getLogger(self.__class__.__name__)

        self.kafka_bootstrap_servers = kafka_bootstrap_servers
        self.kafka_topic_ads = kafka_topic_ads
        self.kafka_topic_active_ids = kafka_topic_active_ids
        
        # Базовая конфигурация для KafkaProducer, если что-то не указано в settings.py
        default_producer_params = {
            'value_serializer': lambda v: json.dumps(v).encode('utf-8'),
            'key_serializer': lambda k: str(k).encode('utf-8') if k else None,
            'retries': 5,
            'retry_backoff_ms': 1000 # milliseconds
        }
        
        # Объединяем конфигурацию из settings.py с базовой.
        # Конфигурация из settings.py имеет приоритет для общих ключей.
        self.kafka_producer_config = {**default_producer_params, **kafka_producer_config}

        # Важно: Убедимся, что 'bootstrap_servers' НЕ находится в self.kafka_producer_config,
        # так как он передается как отдельный именованный аргумент в KafkaProducer в методе open_spider.
        if 'bootstrap_servers' in self.kafka_producer_config:
            self.logger.warning(
                f"KafkaPipeline: 'bootstrap_servers' ('{self.kafka_producer_config['bootstrap_servers']}') обнаружен в KAFKA_PRODUCER_CONFIG из settings.py. "
                f"Он будет удален из KAFKA_PRODUCER_CONFIG, так как используется основной параметр KAFKA_BOOTSTRAP_SERVERS ('{self.kafka_bootstrap_servers}')."
            )
            del self.kafka_producer_config['bootstrap_servers']

    @classmethod
    def from_crawler(cls, crawler):
        # Получаем настройки из settings.py Scrapy
        kafka_bootstrap_servers = crawler.settings.get('KAFKA_BOOTSTRAP_SERVERS')
        kafka_topic_ads = crawler.settings.get('KAFKA_TOPIC_ADS')
        kafka_topic_active_ids = crawler.settings.get('KAFKA_TOPIC_ACTIVE_IDS')

        kafka_producer_config = crawler.settings.get('KAFKA_PRODUCER_CONFIG', {})

        if not kafka_bootstrap_servers or not kafka_topic_ads or not kafka_topic_active_ids: # Added checks for new topics
            raise ValueError(
                "Один из обязательных параметров Kafka (BOOTSTRAP_SERVERS, TOPIC_ADS, TOPIC_ACTIVE_IDS) не настроен в settings.py")

        return cls(
            kafka_bootstrap_servers=kafka_bootstrap_servers,
            kafka_topic_ads=kafka_topic_ads,
            kafka_topic_active_ids=kafka_topic_active_ids,
            kafka_producer_config=kafka_producer_config
        )

    def open_spider(self, spider):
        try:
            # Дополнительный лог для проверки значения self.kafka_bootstrap_servers непосредственно перед использованием
            self.logger.info(f"KafkaPipeline open_spider: Используемые bootstrap_servers: {self.kafka_bootstrap_servers}")
            self.logger.info(f"Попытка инициализации KafkaProducer с серверами: {self.kafka_bootstrap_servers}, конфиг: {self.kafka_producer_config}")
            self.producer = KafkaProducer(
                bootstrap_servers=self.kafka_bootstrap_servers,
                # Удаляем явную передачу value_serializer, так как он уже есть в self.kafka_producer_config
                # value_serializer=lambda v: json.dumps(v).encode('utf-8'), 
                **self.kafka_producer_config
            )
            self.logger.info(f"KafkaProducer успешно инициализирован и подключен к {self.kafka_bootstrap_servers}")
        except KafkaError as e:
            self.logger.error(f"ОШИБКА KafkaError при инициализации KafkaProducer: {e}", exc_info=True)
            self.producer = None # Ensure producer is None on failure
        except Exception as e:
            self.logger.error(f"ОБЩАЯ ОШИБКА (не KafkaError) при инициализации KafkaProducer: {e}", exc_info=True)
            self.producer = None # Ensure producer is None on failure


    def process_item(self, item, spider):
        adapter = ItemAdapter(item)
        
        if isinstance(item, ParsedAdItem):
            # Обработка ParsedAdItem (объявления)
            source_ad_id = adapter.get('source_ad_id')
            if source_ad_id:
                self.producer.send(
                    self.kafka_topic_ads,
                    key=source_ad_id,
                    value=adapter.asdict()
                )
                # self.logger.info(f"Отправлено объявление {source_ad_id} в Kafka.")
            else:
                self.logger.warning("ParsedAdItem без source_ad_id, пропускаем.")
                
        elif isinstance(item, ActiveIdsItem):
            # Обработка ActiveIdsItem (активные ID для марки)
            make_name = adapter.get('make_str')
            ad_ids = adapter.get('ad_ids', [])
            if make_name and ad_ids:
                self.producer.send(
                    self.kafka_topic_active_ids,
                    key=make_name,
                    value=adapter.asdict()
                )
                self.logger.info(f"Отправлен ActiveIdsItem для марки '{make_name}' с {len(ad_ids)} ID в Kafka.")
            else:
                self.logger.warning("ActiveIdsItem без make_str или ad_ids, пропускаем.")
        
        return item

    def close_spider(self, spider):
        # Убираем логику отправки ActiveIdsItem отсюда, так как теперь это делает сам паук
        if self.producer:
            try:
                self.producer.flush()
                self.logger.info("KafkaPipeline: Продюсер Kafka сброшен.")
            except Exception as e:
                self.logger.error(f"KafkaPipeline: Ошибка при сбросе продюсера: {e}")
            finally:
                try:
                    self.producer.close()
                    self.logger.info("KafkaPipeline: Продюсер Kafka закрыт.")
                except Exception as e:
                    self.logger.error(f"KafkaPipeline: Ошибка при закрытии продюсера: {e}")
