# -*- coding: utf-8 -*-
"""
大语言模型(LLM) 工具定义 JSON Schema 完整版
"""

LLM_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_active_layers",
            "description": "获取当前 QGIS 工程中所有图层名称。当你不知道该处理哪个图层时调用此工具。",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "skill_calc_spectral_index",
            "description": "计算遥感影像的各类光谱指数 (NDVI植被, GNDVI, NDWI水体, BSI裸土, NBR火烧等)。",
            "parameters": {
                "type": "object",
                "properties": {
                    "layer_name": {"type": "string", "description": "要处理的栅格图层名称"},
                    "index_type": {"type": "string", "enum": ["ndvi","gndvi","savi","evi","fvc","ndwi","mndwi","ndbi","ndmi","bsi","nbr"]},
                    "b1_idx": {"type": "integer", "description": "波段1的索引 (NDVI中常为NIR即4波段)"},
                    "b2_idx": {"type": "integer", "description": "波段2的索引 (NDVI中常为Red即3波段)"},
                    "b3_idx": {"type": "integer", "description": "波段3的索引 (仅在 EVI/BSI 中需要，默认为1)"}
                },
                "required": ["layer_name", "index_type", "b1_idx", "b2_idx"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "skill_run_pca",
            "description": "对多波段遥感影像进行 PCA 主成分分析降维。",
            "parameters": {
                "type": "object",
                "properties": {
                    "layer_name": {"type": "string"},
                    "n_comp": {"type": "integer", "description": "保留的主成分数量，默认3"}
                },
                "required": ["layer_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "skill_dem_analysis",
            "description": "对 DEM 影像进行地形特征提取 (阴影、坡度、坡向、起伏度)。",
            "parameters": {
                "type": "object",
                "properties": {
                    "layer_name": {"type": "string", "description": "高程 DEM 图层名"},
                    "analysis_type": {"type": "string", "enum": ["hillshade", "slope", "aspect", "TRI"]},
                    "z_factor": {"type": "number", "description": "Z 轴缩放比例，默认 1.0"}
                },
                "required": ["layer_name", "analysis_type"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "skill_spatial_filter",
            "description": "对影像执行空间滤波或边缘提取 (如提取道路/地界线、平滑降噪)。",
            "parameters": {
                "type": "object",
                "properties": {
                    "layer_name": {"type": "string"},
                    "filter_type": {"type": "string", "enum": ["sobel", "gaussian", "laplacian"]},
                    "band_idx": {"type": "integer", "description": "处理的波段序号，默认1"}
                },
                "required": ["layer_name", "filter_type"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "skill_area_statistics",
            "description": "统计分类图层中各类别的像元数量、面积(平方米与亩数)及占比。",
            "parameters": {
                "type": "object",
                "properties": {"layer_name": {"type": "string", "description": "分类/解译图层名"}},
                "required": ["layer_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "skill_vector_smooth",
            "description": "对矢量面图层(多边形)进行化简去锯齿和平滑处理。",
            "parameters": {
                "type": "object",
                "properties": {
                    "layer_name": {"type": "string", "description": "要平滑的矢量图层"},
                    "tolerance": {"type": "number", "description": "化简容差，默认 1.0"},
                    "iterations": {"type": "integer", "description": "平滑迭代次数，默认 2"}
                },
                "required": ["layer_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "skill_kmeans_cluster",
            "description": "对影像进行 K-Means 智能无监督聚类。",
            "parameters": {
                "type": "object",
                "properties": {
                    "layer_name": {"type": "string"},
                    "k": {"type": "integer", "description": "聚类类别数量", "default": 5},
                    "max_iters": {"type": "integer", "description": "最大迭代次数", "default": 15}
                },
                "required": ["layer_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "skill_raster_diff",
            "description": "传统双期影像像元差分变化检测 (绝对差值提取变化区域)。",
            "parameters": {
                "type": "object",
                "properties": {
                    "layer_t1": {"type": "string", "description": "基准期影像T1"},
                    "layer_t2": {"type": "string", "description": "变化期影像T2"},
                    "threshold": {"type": "number", "description": "变化灵敏度阈值", "default": 30.0},
                    "polygonize": {"type": "boolean", "description": "是否顺便输出矢量图斑"}
                },
                "required": ["layer_t1", "layer_t2"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "skill_image_enhance",
            "description": "将影像按指定波段组合为RGB，并应用对比度拉伸提升画质 (如假彩色合成)。",
            "parameters": {
                "type": "object",
                "properties": {
                    "layer_name": {"type": "string"},
                    "r": {"type": "integer", "description": "红通道波段 (默认4)"},
                    "g": {"type": "integer", "description": "绿通道波段 (默认3)"},
                    "b": {"type": "integer", "description": "蓝通道波段 (默认2)"}
                },
                "required": ["layer_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "skill_raster_polygonize",
            "description": "将掩膜/分类结果栅格转换为矢量多边形图斑，并过滤孤立噪点。",
            "parameters": {
                "type": "object",
                "properties": {
                    "layer_name": {"type": "string"},
                    "sieve_size": {"type": "integer", "description": "过滤碎斑大小阈值", "default": 4}
                },
                "required": ["layer_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "skill_ai_extract_feature",
            "description": "向云端投递 AI 大模型解译任务，可以提取特定地物（建筑、水体、道路、林地、草地、耕地、施工）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "layer_name": {"type": "string"},
                    "feature_type": {"type": "string", "enum": ["建筑", "水体", "道路", "林地", "草地","耕地", "施工"]}
                },
                "required": ["layer_name", "feature_type"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "skill_ai_sam3_extract",
            "description": "基于 SAM3 大语言模型的万物识别。当用户要求用英文 Prompt 提示词提取某个没见过的物体，或要求做目标检测画框(bbox)时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "layer_name": {"type": "string"},
                    "prompt": {"type": "string", "description": "输入的英文提示词(如: solar panel, swimming pool, ship)"},
                    "output_format": {"type": "string", "enum": ["mask", "bbox"], "description": "mask表示分割面，bbox表示目标检测方框"}
                },
                "required": ["layer_name", "prompt", "output_format"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "skill_ai_change_detection",
            "description": "利用 AI 深度模型比较两张不同时相的影像，提取新增、拆除等地表变化。",
            "parameters": {
                "type": "object",
                "properties": {
                    "layer_t1": {"type": "string", "description": "早期/基准期图层名"},
                    "layer_t2": {"type": "string", "description": "后期/变化期图层名"}
                },
                "required": ["layer_t1", "layer_t2"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "skill_geocode_address",
            "description": "地址地理编码与QGIS画布定位工具。支持国内外地址：【国内地址】仅需提供 address_text，后台将自动调用天地图进行高精度地理编码；【国外/境外地名】（如埃菲尔铁塔、东京塔、纽约时代广场等），请大模型基于自身地理知识库，在调用本工具时一并传入预测的 WGS84 经度(lon)和纬度(lat)。最终在QGIS中生成点矢量图层并居中定位。",
            "parameters": {
                "type": "object",
                "properties": {
                    "address_text": {
                        "type": "string",
                        "description": "地名或地址文本，例如：'北京市海淀区中关村'、'法国巴黎埃菲尔铁塔'"
                    },
                    "lon": {
                        "type": "number",
                        "description": "目标地点的WGS84经度（浮点数）。国外地名或天地图无数据的地名时，由模型直接推算填入；国内地址可不填。"
                    },
                    "lat": {
                        "type": "number",
                        "description": "目标地点的WGS84纬度（浮点数）。国外地名或天地图无数据的地名时，由模型直接推算填入；国内地址可不填。"
                    }
                },
                "required": ["address_text"]
            }
        }
    }
]