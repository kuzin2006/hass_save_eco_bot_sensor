# Home Assistant SaveEcoBot API Sensor

from pydantic import BaseModel
from pydantic.error_wrappers import ValidationError
from typing import List, Optional
import datetime
from enum import Enum
from copy import deepcopy
import logging
import voluptuous as vol
from homeassistant.helpers.entity import Entity
from homeassistant.exceptions import PlatformNotReady
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.config_validation import PLATFORM_SCHEMA
import asyncio
import aiohttp

from homeassistant.const import (
    CONCENTRATION_MILLIGRAMS_PER_CUBIC_METER,
    TEMP_CELSIUS,
    PRESSURE_HPA,
    PERCENTAGE,
)

DOMAIN = "save_eco_bot"
SENSOR_DEPRECATION_HOURS = 12

_LOGGER = logging.getLogger(__name__)

CONF_STATION_IDS = "station_ids"
CONF_CITY_NAMES = "city_names"
CONF_STATION_NAMES = "station_names"

SERVICE_SHOW_CITIES = "show_cities"
SERVICE_SHOW_CITY_STATIONS = "show_city_stations"

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Optional(CONF_STATION_IDS): cv.ensure_list,
        vol.Optional(CONF_CITY_NAMES): cv.ensure_list,
        vol.Optional(CONF_STATION_NAMES): cv.ensure_list,
    }
)


class PollutantType(Enum):
    PM2_5 = "PM2.5"
    PM10 = "PM10"
    TEMPERATURE = "Temperature"
    HUMIDITY = "Humidity"
    PRESSURE = "Pressure"
    AQI = "Air Quality Index"


class Pollutant(BaseModel):
    """
    pollutants from API response
    """
    pol: PollutantType
    unit: str
    time: Optional[datetime.datetime]
    value: Optional[float]
    averaging: str

    @property
    def hass_unit(self):
        """
        unify returned unit with HASS standards
        """
        _units_translation = {
            "mg/m3": CONCENTRATION_MILLIGRAMS_PER_CUBIC_METER,
            "Celcius": TEMP_CELSIUS,
            "%": PERCENTAGE,
            "hPa": PRESSURE_HPA,
        }
        return _units_translation[self.unit] if self.unit in _units_translation.keys() else self.unit


class SaveEcoBotSensorModel(BaseModel):
    """
    represents data model for HASS sensor
    """
    name: str
    unique_id: str
    station_id: str
    sensor_type: PollutantType
    state: float
    device_state_attributes: dict
    deprecated: bool = True


class Station(BaseModel):
    """
    SaveEcoBot station Model
    """
    id: str
    cityName: str
    stationName: str
    localName: str
    timezone: str
    latitude: float
    longitude: float
    pollutants: List[Pollutant]

    @property
    def slug(self):
        return f"{self.id}_{self.cityName}".lower()

    def sensors(self) -> List[SaveEcoBotSensorModel]:
        """
        returns sensors data as required by HASS, name, value, attrs
        """
        station_sensors: List[SaveEcoBotSensorModel] = []
        common_attrs = {
            "city": self.cityName,
            "address": self.stationName,
            "local_name": self.localName,
            "timezone": self.timezone,
            "latitude": self.latitude,
            "longitude": self.longitude,
        }
        for p in self.pollutants:
            attrs = {
                "updated_at": datetime.datetime.strftime(p.time, "%d.%m.%Y, %H:%M:%S"),
                "unit_of_measurement": p.hass_unit,
                "averaging": p.averaging,
                **common_attrs
            }
            station_sensor = SaveEcoBotSensorModel(
                name=f"{p.pol.name} ({self.cityName}, {self.stationName})",
                unique_id=f"{self.slug}_{p.pol.name.lower()}",
                station_id=self.id,
                sensor_type=p.pol,
                state=p.value,
                device_state_attributes=attrs,
                deprecated=datetime.datetime.now() - p.time > datetime.timedelta(hours=SENSOR_DEPRECATION_HOURS)
            )
            station_sensors.append(station_sensor)
        return station_sensors


class StationsClient:
    """
    represents a parsed set of SaveEcoBot stations
    and methods for its parsing
    """

    _api_url = "https://api.saveecobot.com/output.json"

    def __init__(self):
        """
        parse and validate JSON response from API server
        """
        self.stations: List[Station] = []
        self.updated_at = datetime.datetime.now()

    # service call: manual UPDATE
    async def update(self, force=False):
        # make API calls once per 30 seconds
        if datetime.datetime.now() - self.updated_at < datetime.timedelta(seconds=30) and not force:
            _LOGGER.debug("Update called, using cached values")
            return True
        _LOGGER.info("Performing SaveEcoBot API call...")
        async with aiohttp.ClientSession() as session:
            async with session.get(self._api_url) as resp:
                if resp.status != 200:
                    _LOGGER.error(f"failed API response {resp.status}:{resp.content}")
                    return False
                stations_resp = await resp.json()

        self.stations = []
        for station in stations_resp:
            try:
                self.stations.append(Station(**station))
            except ValidationError as e:
                _LOGGER.error(f"Validation error {e}, skipping: {station}")
                continue
        self.updated_at = datetime.datetime.now()
        _LOGGER.debug("Updated from API call.")
        return True

    def filter_stations(self, station_ids=[], city_names=[], station_names=[]) -> iter:
        """
        filter stations list by given field name and its value
        allowed filter fields are id, cityName, stationName
        """
        _filters = {
            'station_id': lambda s: s.id in station_ids,
            'city_name': lambda s: s.cityName in city_names,
            'station_name': lambda s: s.stationName in station_names
        }

        fs = deepcopy(self.stations)

        if station_ids:
            fs = filter(_filters['station_id'], fs)
        if city_names:
            fs = filter(_filters['city_name'], fs)
        if station_names:
            fs = filter(_filters['station_name'], fs)

        return fs

    # service call `save_eco_bot.cities`
    def cities(self) -> list:
        """
        returns cities available in stations set
        """
        available_cities = set(c.cityName for c in self.stations)
        return sorted(available_cities)

    # service call `save_eco_bot.city_station`
    # payload:
    #     city: <city_name>
    def city_stations(self, city: str) -> List:
        """
        returns short stations data in given city:
        {
            <city>: [
                (id, station_name),
                (id, station_name),
                ....
                ]
            }
        """
        if city not in self.cities():
            return []

        city_stations = self.filter_stations(city_names=[city])

        return [(c.id, c.stationName) for c in city_stations]

    def get_sensor(self, station_id: str, sensor_type: PollutantType) -> SaveEcoBotSensorModel:
        """
        return sensor data
        """
        station = list(self.filter_stations(station_ids=[station_id])).pop()
        sensor = list(filter(lambda p: p.sensor_type is sensor_type, station.sensors()))

        return sensor.pop() if sensor else None


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up the sensor platform."""
    _client = StationsClient()

    try:
        await _client.update(force=True)
    except (
            aiohttp.client_exceptions.ClientConnectorError,
            asyncio.TimeoutError,
    ) as err:
        _LOGGER.exception("Failed to connect to SaveEcoBot servers")
        raise PlatformNotReady from err

    station_ids = config.get(CONF_STATION_IDS)
    city_names = config.get(CONF_CITY_NAMES)
    station_names = config.get(CONF_STATION_NAMES)

    stations = _client.filter_stations(station_ids=station_ids, city_names=city_names, station_names=station_names)
    sensors = []
    for station in stations:
        sensors += [SaveEcoBotSensor(client=_client, sensor_model=s) for s in station.sensors()]
    async_add_entities(sensors)
    _LOGGER.debug(f"Setup of SaveEcoBot platform is done. {len(sensors)} sensors added.")

    async def show_cities_handler(_):
        _cities = '\n'.join(_client.cities())
        _LOGGER.debug(f"`show_cities` service called. Cities are: {_cities}")
        hass.components.persistent_notification.async_create(
            f"Available cities: \n {_cities}",
            title="SaveEcoBot Cities",
            notification_id="save_eco_bot_show_cities",
        )

    async def show_city_stations_handler(service):
        _LOGGER.debug(f"`show_city_stations` service called. Data: {service.data}")
        _city = service.data.get("city", "<please provide `city: city_name` in service data>")
        _data = _client.city_stations(_city)

        _message = '\n'.join([f"{s_id} - {s_addr}" for s_id, s_addr in _data]) if _data else ''

        hass.components.persistent_notification.async_create(
            f"Stations in {_city}:\n\n {_message}",
            title="SaveEcoBot Stations",
            notification_id="save_eco_bot_show_city_stations",
        )

    hass.services.async_register(
        DOMAIN, SERVICE_SHOW_CITIES, show_cities_handler
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SHOW_CITY_STATIONS, show_city_stations_handler
    )


class SaveEcoBotSensor(Entity):
    """Representation of a Sensor."""

    def __init__(self, client: StationsClient, sensor_model: SaveEcoBotSensorModel):
        """Initialize the sensor."""
        self._client = client
        self._model = sensor_model
        self._name = self._model.name

    @property
    def name(self):
        """Return the name of the sensor."""
        return self._name

    @property
    def state(self):
        """Return the state of the sensor."""
        if self._model.deprecated:
            return "deprecated"
        return self._model.state if self._model else "unknown"

    @property
    def unit_of_measurement(self):
        """Return the unit of measurement."""
        return self._model.device_state_attributes["unit_of_measurement"]

    @property
    def device_state_attributes(self):
        """attributes"""
        return self._model.device_state_attributes

    async def async_update(self):
        """Fetch new state data for the sensor.
        This is the only method that should fetch new data for Home Assistant.
        """
        await self._client.update()
        self._model = self._client.get_sensor(station_id=self._model.station_id, sensor_type=self._model.sensor_type)
        if self._model is None:
            _LOGGER.error(f"Error updating data from sensor {self._name}! Got no data from API.")
            return
        _LOGGER.debug(f"Updated: {self.name}")

