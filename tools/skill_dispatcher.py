# -*- coding: utf-8 -*-
"""
LLM skill dispatcher — compatibility facade.

The implementation now lives in the :mod:`tools.skills` subpackage:

- ``tools.skills.common``        shared spatial helpers
- ``tools.skills.layers``        layer profiling / project report
- ``tools.skills.geocode``       address geocoding & canvas focus
- ``tools.skills.fetch_imagery`` Sentinel-2 / DEM / Landsat / SAR streaming
- ``tools.skills.fetch_vector``  OpenStreetMap Overpass vectors
- ``tools.skills.fetch_thematic``Natural Earth / WorldCover / WorldPop / ERA5 ...
- ``tools.skills.analysis``      local raster/vector analysis operators
- ``tools.skills.ai_tasks``      cloud AI interpretation task factories
- ``tools.skills.qgis_ops``      native Processing exec + guarded dynamic PyQGIS
- ``tools.skills.web``           web search and page extraction

Executable skills are whitelisted in :mod:`tools.skill_registry` (populated
by importing ``tools.skills``); UI code must not resolve names via getattr.
"""
from .skills import (  # noqa: F401
    get_layer_by_name,
    _inspect_raster_profile,
    _validate_bbox,
    _get_target_bbox,
)
from .skills.layers import get_active_layers  # noqa: F401
from .skills.geocode import skill_geocode_address  # noqa: F401

from .skills.fetch_imagery import (  # noqa: F401
    STAC_AWS_URL,
    search_and_load_sentinel2,
    skill_fetch_sentinel2_imagery,
    skill_fetch_dem_data,
    skill_fetch_landsat_imagery,
    skill_fetch_sentinel1_sar,
)

from .skills.fetch_vector import OVERPASS_SERVERS, skill_fetch_osm_vector_data  # noqa: F401

from .skills.fetch_thematic import (  # noqa: F401
    skill_fetch_natural_earth,
    skill_fetch_worldcover_lulc,
    skill_fetch_worldpop_density,
    skill_fetch_nighttime_lights,
    skill_fetch_hydrology_data,
    skill_fetch_era5_climate,
)

from .skills.analysis import (  # noqa: F401
    skill_raster_threshold,
    skill_run_pca,
    skill_dem_analysis,
    skill_spatial_filter,
    skill_area_statistics,
    skill_vector_smooth,
    skill_kmeans_cluster,
    skill_raster_diff,
    skill_image_enhance,
    skill_raster_polygonize,
)

from .skills.ai_tasks import (  # noqa: F401
    skill_ai_extract_feature,
    skill_ai_sam3_extract,
    skill_ai_change_detection,
)

from .skills.qgis_ops import (  # noqa: F401
    qgis_search_tools,
    qgis_get_tool_params,
    qgis_run_algorithm,
    execute_pyqgis_code,
)

from .skills.web import (  # noqa: F401
    skill_web_search,
    skill_fetch_webpage_content,
)

# Importing this module guarantees the whitelist is populated.
from .skill_registry import (  # noqa: F401,E402
    register as _register,
    get_skill,
    all_skills,
    skill_label,
)
