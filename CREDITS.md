# Credits

## Models
- **OutfitTransformer** (outfit compatibility, `backend/app/compat/`): Sarkar et al., *OutfitTransformer: Learning
  Outfit Representations for Fashion Recommendation*, WACV 2023. Community re-implementation and the CLIP-variant
  compatibility checkpoint by **owj0421**: https://github.com/owj0421/outfit-transformer (MIT License).
- **FashionCLIP** `patrickjohncyh/fashion-clip` (image/text embeddings, redundancy, zero-shot attributes):
  Chia et al., *Contrastive language and vision learning of general fashion concepts*, Scientific Reports 2022.
  https://huggingface.co/patrickjohncyh/fashion-clip (MIT License).
- **SegFormer B2 clothes** `mattmdjaga/segformer_b2_clothes` (garment cutouts): SegFormer (Xie et al., NeurIPS 2021)
  fine-tuned on the ATR human-parsing dataset. https://huggingface.co/mattmdjaga/segformer_b2_clothes. License per
  the model card: NVIDIA SegFormer license (https://github.com/NVlabs/SegFormer/blob/master/LICENSE), which permits
  research / non-commercial use.
- **Google Gemini** (`gemini-3-flash-preview` and fallbacks) via the Gemini API: garment detection, attributes, and
  shopping-query planning / Google Search grounding for suggestions.

## Data
- **Polyvore Outfits** (Vasileva et al., *Learning Type-Aware Embeddings for Fashion Compatibility*, ECCV 2018), used
  via Hugging Face `owj0421/polyvore` and `owj0421/polyvore-outfits` (license: "other", research dataset). It provides
  the demo closet, the seeded candidates, `backend/data/test_images/product_*.jpg`, and OutfitTransformer's training data.
- Test photos (Creative Commons / CC0, Flickr, Openverse, rawpixel): see `backend/data/test_images/CREDITS.md`.
- Shopping suggestions show live product names, prices, photos and links from the retailers' public storefronts
  (e.g. Everlane, Princess Polly, Good American, Marine Layer, tentree). Those belong to the retailers; FitCheck only
  links to them.

## Sustainability data
Fibre carbon/water factors: WRAP (2012) *Valuing our clothes* (Table 3), plus WRAP figures as reproduced by Ethical Consumer (2024) for linen, silk and polyurethane.
Organic cotton: Textile Exchange (2014). Recycled polyester: Carbonfact, Swiss FOEN (2017). Silk water: Astudillo et al. (2015). Leather: Brugnoli et al. (2025), LWG (2024).
Garment weights: Ecobalyse (French Ministry of Ecological Transition / ADEME), Sandin et al. (2019, Mistra Future Fashion), Cotton Incorporated (2019).
Default wears: draft PEFCR Apparel & Footwear v1.1 (2021). Footwear: Cheah et al. (2013, MIT), Bodoga et al. (2024).
Use-phase evidence: WRAP (2012, 2014), Ellen MacArthur Foundation (2017), Laitala et al. (2018), Quantis (2018). Full list: docs/SUSTAINABILITY.md.

## Software
FastAPI, PyTorch, Hugging Face Transformers, FAISS, Pillow, httpx, google-genai; Next.js, React, Tailwind CSS,
lucide-react. Each is under its own open-source license.
