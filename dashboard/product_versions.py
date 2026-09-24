DEFAULT_PRODUCT_VERSION = "M1"
PRODUCT_VERSION_SCENES = {
    "M1": "488",
    "M2": "904",
}
SCENE_PRODUCT_VERSIONS = {scene_id: version for version, scene_id in PRODUCT_VERSION_SCENES.items()}
PRODUCT_VERSION_CHOICES = tuple((version, version) for version in PRODUCT_VERSION_SCENES)


def product_version_for_scene(scene_id: str) -> str | None:
    return SCENE_PRODUCT_VERSIONS.get(str(scene_id))


def is_product_version(value: object) -> bool:
    return isinstance(value, str) and value in PRODUCT_VERSION_SCENES
