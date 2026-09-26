"""Structured garment details used by the clean-image renderers (app/render.py, app/render_template.py).

Filled either inside the normal Gemini detection call (DetectedItem.details, boxes relative to the whole photo) or by
ONE batched vision call over several item crops (render_template.fetch_details, boxes relative to each crop)."""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class GarmentType(str, Enum):
    tshirt = "tshirt"; longsleeve_tee = "longsleeve_tee"; sweater = "sweater"; sweatshirt = "sweatshirt"
    polo = "polo"; shirt = "shirt"; tank_top = "tank_top"; hoodie = "hoodie"; zip_hoodie = "zip_hoodie"
    jacket = "jacket"; coat = "coat"; jeans = "jeans"; trousers = "trousers"; shorts = "shorts"; skirt = "skirt"
    dress = "dress"; shoes = "shoes"; other = "other"  # no accessories: FitCheck handles clothes and shoes only


class Graphic(BaseModel):
    box_2d: list[int] = Field(description="[ymin, xmin, ymax, xmax] 0-1000, tight around the logo/text/graphic/patch")
    kind: str = Field(description="logo | text | graphic | patch | label | embroidery")
    text: str = Field(description="exact visible text/letters, '' if none or unreadable")
    position: str = Field(description="where it sits ON THE GARMENT: chest_center | left_chest | right_chest | "
                                      "full_front | upper_back | back_center | back_waistband_right | "
                                      "back_waistband_left | back_pocket | front_waist | sleeve | hem | other "
                                      "(left/right = the WEARER's left/right)")
    rotate_cw_deg: int = Field(description="degrees to rotate the graphic CLOCKWISE so it is upright/readable as "
                                           "printed on the garment (0 if already upright; e.g. 90, -20, 180)")
    description: str = Field(description="short description, e.g. 'white circular cross emblem'")


class GarmentDetails(BaseModel):
    garment_type: GarmentType
    view: str = Field(description="which side of the garment faces the camera: front | back")
    sleeve_length: str = Field(description="short | long | three_quarter | sleeveless | none")
    neckline: str = Field(description="crew | v_neck | polo_collar | shirt_collar | hood | scoop | turtleneck | "
                                      "mock_neck | strapless | none")
    closure: str = Field(description="none | full_zip | half_zip | buttons")
    has_hood: bool
    has_front_pockets: bool = Field(description="kangaroo / patch / slash pockets visible or expected on the front")
    ribbed_hem_cuffs: bool = Field(description="ribbed waistband and cuffs (sweatshirts, hoodies, sweaters)")
    has_drawstrings: Optional[bool] = Field(default=None, description="visible drawstrings/cords (hood or waist); "
                                                                      "false if the hood/waist has none")
    fit: str = Field(description="slim | regular | relaxed | oversized")
    length: str = Field(description="cropped | regular | long | mini | midi | maxi")
    lining_color: Optional[str] = Field(default=None, description="hood lining colour if visibly different, else null")
    hardware_color: Optional[str] = Field(default=None, description="zip/buttons/rivets colour (e.g. silver, brass, "
                                                                    "black, tonal), else null")
    stitch_color: Optional[str] = Field(default=None, description="contrast topstitching colour (e.g. gold on "
                                                                  "denim), else null")
    graphics: list[Graphic] = Field(default_factory=list, description="every logo, printed/embroidered text, graphic, "
                                                                      "brand patch on the garment (empty if plain)")


class BatchItemDetails(GarmentDetails):
    index: int = Field(description="the item number given before its image")


class BatchDetails(BaseModel):
    items: list[BatchItemDetails]


BATCH_PROMPT = """You are a fashion catalogue assistant preparing brand-style product images.
Each numbered image below is a photo crop of ONE garment (named before it). Other garments may be partly visible:
describe ONLY the named garment. For each item return its construction details and EVERY logo / printed text /
graphic / brand patch on it with a tight box_2d [ymin, xmin, ymax, xmax] normalised 0-1000 RELATIVE TO THAT IMAGE,
the exact text (spelling as printed; '' if none), where it sits on the garment (wearer's left/right), and how many
degrees it must be rotated CLOCKWISE to be upright as printed on the garment. Say whether the photo shows the front or
the back of the garment. Do not invent logos that are not visible."""
