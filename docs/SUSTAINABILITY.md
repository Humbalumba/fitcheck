# FitCheck sustainability score

`backend/app/sustainability.py` estimates the footprint of a candidate item and how well it would be used in
**this** wardrobe. It's pure Python: no network, no Gemini, no DB. Every factor comes from
`backend/app/data/sustainability_factors.json`, which gives each number its source, URL, year and a note.
All outputs carry `"is_estimate": true`.

```python
from app.sustainability import score_item
res = score_item(cand["attributes"], cand["category"], n_new_outfits=result["total_new_outfits"],
                 redundancy=result["redundancy"]["top_similarity"], price=price)
# optional: dup_threshold=settings["redundancy_duplicate_threshold"], similar_threshold=settings["redundancy_similar_threshold"]
```
`redundancy` can be the top similarity (float) or the app's level string (`none | similar | near_duplicate`).
Accessories (and items with no category) return `supported: false, score: null`.

Returned keys: `score` (0-100), `grade` (A-E), `label` (Great/Good/Fair/Poor/Very poor), `reasons` (2 plain-English
sentences), `footprint_kg_co2e`, `water_l` (+`water_complete`), `expected_wears`, `per_wear_kg_co2e`,
`per_wear_water_l`, `cost_per_wear`, `garment_type`, `footprint_basis` (`per_kg` or `per_pair`), `weight_kg`,
`materials` (the parsed mix with per-kg factors), `material_parse`, `components` (every intermediate number),
`methodology`, `sources` (keys into the JSON), `version`.

## Formula

The expected-wears part (`expected_wears()` in the same module) is shared with the BUY / CONSIDER / SKIP verdict, which
uses it for **cost per wear** (see `backend/README.md`), so the verdict and this card always agree on wears.

| step | formula | status |
|---|---|---|
| material mix | parse `material` / `fabric_guess` into shares: `95% cotton 5% elastane` becomes {cotton .95, elastane .05}. With no %, recognised fibres are split equally. `X blend` = 50% X + 50% average textile. `polycotton` = 50/50. Unlisted % goes to the average textile. Nothing recognised = average textile. If the material field is empty, fibre words in the description are used. | parsing rules = assumption |
| footprint | `weight_kg(type) × Σ share_i × CO2e_per_kg_i`; **shoes:** a per-pair LCA value | **sourced** factors |
| water | `weight_kg(type) × Σ share_i × L_per_kg_i` over materials that have a water factor (`water_complete=false` otherwise); shoes: not estimated | **sourced** factors |
| base wears | PEFCR Apparel & Footwear default wears for the type (T-shirt 45 … coat 100) | **sourced** |
| utility | `u(n) = min(2.0, 0.5 + 0.25·log2(1+n))` → 0.5× at 0 new outfits, 1.0× at 3, +0.25 per doubling, capped at 2.0× (at about 63 outfits) | **assumption** (the 4× spread is bounded by EMF 2017: US clothes are worn about ¼ of the global average) |
| redundancy | ×1.0 none, ×0.75 similar (≥ 0.80), ×0.5 near-duplicate (≥ 0.88): two interchangeable items split the same occasions | **assumption** (thresholds = the app's) |
| expected wears | `base × u(n) × redundancy` | derived |
| per wear | `footprint / expected_wears` (same for water) | derived |
| score | `relative = per_wear / per_wear_typical`, where *typical* = same type, same weight, **average textile** (21.6 kg CO2e/kg), base wears (shoes: same per-pair value). `score = clamp(round(50 − 30·log2(relative)), 0, 100)`. 50 = a typical item of that type used typically. Each halving of per-wear CO2e adds 30 points. | **assumption** (scale) |
| grade | A ≥ 80 Great · B ≥ 65 Good · C ≥ 50 Fair · D ≥ 35 Poor · E < 35 Very poor | assumption |

Why per wear: WRAP (2012, Table 4) found that 33% longer active life (9 months) cuts carbon by 27%, water by 33% and
waste by 22%. That matches footprint-per-wear scaling as 1/wears (+33% wears gives −25% per wear). So "how much
will you actually wear it" is the biggest lever. FitCheck can predict it from the closet (new outfits, redundancy).

## Material factors (per kg of fibre)

| material (keywords mapped) | kg CO2e / kg | water L / kg | source | range / note |
|---|---|---|---|---|
| cotton (denim, jersey, piqué, twill, poplin, corduroy, flannel…) | 28 | 3,100 | WRAP 2012 *Valuing our clothes*, Table 3 | Nordic 2014: 13.9 kg, 5,597 L (green+blue) |
| organic cotton | 27.2 | 1,162 | derived: WRAP cotton minus fibre-stage gap in Textile Exchange 2014 organic-cotton LCA (1,808 → 978 kg CO2e/t; blue water 2,120 → 182 m³/t) | TE says the comparison is indicative only |
| polyester (satin, fleece, chiffon, "synthetic") | 21 | 80 | WRAP 2012 Table 3 | Nordic 2014: 16.9 kg, 78 L. Fossil-based, sheds microfibres (not in CO2e) |
| recycled polyester | 19.0 | 80 | derived: WRAP polyester minus the fibre-stage saving (Carbonfact: virgin PET 3.12 vs mechanically recycled 0.68-1.56 kg/kg) | FOEN 2017 cites ~32% lower CO2 at fibre level. Fibre is only ~15% of apparel GHG (Quantis 2018). No sourced water difference |
| nylon / polyamide | 24 | 80 | WRAP 2012 Table 3 | Nordic 2014: 20.2 kg, 78 L |
| acrylic | 38 | 130 | WRAP 2012 Table 3 | Nordic 2014: 35.4 kg, 128 L |
| wool (merino, cashmere, alpaca, tweed…) | 46 | 2,200 | WRAP 2012 Table 3 | Nordic 2014: 44.4 kg, 16,379 L |
| viscose / rayon / modal / lyocell / bamboo | 30 | 3,800 | WRAP 2012 Table 3 | Nordic 2014: 26.4 kg, 3,829 L. Lyocell is usually lower (not separated) |
| linen / flax (hemp as proxy) | 15 | not estimated | WRAP 2012 figures as reproduced by Ethical Consumer 2024 | No litres-per-kg figure on the same basis. European flax is mostly rain-fed |
| silk | 25 | 24,600 (blue) | Ethical Consumer 2024 (WRAP figures); water: Astudillo et al. 2015 | Astudillo raw silk: 51.5 kg CO2e/kg. Silk water ranges from ~445 to >25,000 L/kg |
| elastane / spandex / PU / faux leather → "polyurethane" | 20 | 80 (polyester proxy) | Ethical Consumer 2024 (WRAP figures) | Elastane is ~85% segmented polyurethane. Water uses the closest sourced synthetic |
| leather / suede (garments only) | 22.0 | not estimated | Brugnoli et al. 2025 (industry primary data) | Depends heavily on allocation: Roncevich et al. 2026 get 187.1 kg/kg; LWG 2024: 22.48 kg/m² |
| unknown / unrecognised (the "average textile") | 21.6 | 3,580 | derived from WRAP 2012 totals: 38 Mt CO2e and 6,300 Mm³ ÷ 1.76 Mt raw materials | Nordic 2014 "average textile": 21 kg, 4,527 L |

**Basis:** WRAP's per-fibre numbers are *whole-life-cycle* footprints per kg of fibre in UK clothing. Production is
about ¾ and laundry about ¼. They aren't strict cradle-to-gate values. We use one source on purpose, so the
material comparisons stay consistent. WRAP itself says data on water use for synthetic fibres is thin.

## Garment types (weight, base wears)

| type (subcategory keywords) | weight kg | source | base wears (PEFCR 2021 Table 6) | cross-checks |
|---|---|---|---|---|
| tshirt (t-shirt, tee, tank, cami, polo, generic top) | 0.15 | Ecobalyse example | 45 | Cotton Inc 2019: 0.16 kg. Sandin 2019: 110 g, 30 uses. WRAP 2014: ~82 wears |
| shirt (shirt, blouse, button-down) | 0.25 | Ecobalyse example | 40 | WRAP 2014: ~58 wears |
| sweater (sweater, hoodie, sweatshirt, cardigan, fleece) | 0.55 | Ecobalyse example | 85 | WRAP 2014 knitwear: ~111 wears |
| trousers (trousers, pants, chinos, leggings, joggers) | 0.45 | Ecobalyse example | 70 | Cotton Inc 2019 woven bottoms: 0.49 kg (men) / 0.38 kg (women) |
| jeans | 0.45 | Ecobalyse example | 70 | Sandin 2019: 477 g, 240 uses. Carlsson 2011: 700 g. WRAP 2014: ~232 wears |
| shorts | 0.45 | Ecobalyse ("pantalon / short") | 70 | no separate sourced weight, so likely an over-estimate |
| skirt | 0.30 | Ecobalyse ("jupe / robe") | 70 | |
| dress (dress, jumpsuit) | 0.30 | Ecobalyse ("jupe / robe") | 70 | Sandin 2019: 478 g lined dress, 26 uses |
| jacket (jacket, blazer, bomber, vest) | 0.444 | Sandin 2019 | 100 | Sandin: 140 uses (an unsupported assumption, per the authors) |
| coat (coat, parka, puffer, trench) | 0.95 | Ecobalyse ("manteau / veste") | 100 | |
| sneakers (+ loafers, heels, flats) | **14.0 kg CO2e / pair** | Cheah et al. 2013 (MIT) | 100 (closed-toed) | PUMA / Timberland: 18-41 kg per pair |
| boots | **18.65 kg CO2e / pair** | Bodoga et al. 2024 | 100 | GORE-TEX hiking boots: 27.1 kg |
| sandals | **14.0 kg CO2e / pair** (running-shoe proxy) | Cheah et al. 2013 | 50 (open-toed) | no sourced sandal figure, so likely an over-estimate |

## Example outputs

Numbers come straight from `score_item`. The trousers' and cami's outfit counts and similarities match the seeded
demo candidates in API.md. The seeded items have no `fabric_guess`, so the "as seeded" rows show the fallback.

| Example | type | material mix | kg CO2e | water L | exp. wears | kg CO2e / wear | vs typical | score | $ / wear |
|---|---|---|---|---|---|---|---|---|---|
| White cotton trousers — 42 outfits, top match 0.798, $45 | trousers | 100% cotton | 12.6 | 1395 | 130 | 0.097 | 0.70 | **66 B** (Good) | $0.35 |
| Same trousers, material unknown (as seeded) | trousers | 100% unknown | 9.7 | 1611 | 130 | 0.075 | 0.54 | **77 B** (Good) | $0.35 |
| White linen trousers — 42 outfits | trousers | 100% linen | 6.8 | – | 130 | 0.052 | 0.37 | **93 A** (Great) | $0.35 |
| Black polyester cami — near-dup 0.898, 3 outfits, $28 | tshirt | 100% polyester | 3.1 | 12 | 22 | 0.140 | 1.94 | **21 E** (Very poor) | $1.24 |
| Black cami as seeded — near-dup 0.898, 30 outfits, material unknown | tshirt | 100% unknown | 3.2 | 537 | 39 | 0.083 | 1.15 | **44 D** (Poor) | $0.72 |
| Wool coat — 10 outfits, no similar item, $180 | coat | 100% wool | 43.7 | 2090 | 136 | 0.320 | 1.56 | **31 E** (Very poor) | $1.32 |
| Leather ankle boots — 20 outfits, $150 | boots | per pair | 18.6 | – | 160 | 0.117 | 0.63 | **70 B** (Good) | $0.94 |
| T-shirt 95% cotton 5% elastane — similar 0.83, 15 outfits, $20 | tshirt | 95% cotton, 5% polyurethane | 4.1 | 442 | 51 | 0.082 | 1.14 | **44 D** (Poor) | $0.40 |
| Recycled-polyester dress — 8 outfits, $60 | dress | 100% recycled_polyester | 5.7 | 24 | 90 | 0.063 | 0.68 | **67 B** (Good) | $0.66 |
| Acrylic sweater — 0 outfits, $35 | sweater | 100% acrylic | 20.9 | 72 | 42 | 0.492 | 3.52 | **0 E** (Very poor) | $0.82 |

- **White cotton trousers — 42 outfits, top match 0.798, $45**
  - Unlocks 42 outfits, so its ~12.6 kg CO2e is spread over ~130 expected wears (~0.10 kg per wear).
  - Cotton has a higher carbon footprint per kg than an average textile (28 vs 22 kg CO2e/kg), and it's water-intensive (~3,100 L/kg).
- **Black polyester cami — near-dup 0.898, 3 outfits, $28**
  - Near-duplicate of something you already own (0.90 match), so the two would share the same occasions: ~3.1 kg CO2e over only ~22 expected wears (~0.14 kg per wear).
  - Polyester has roughly the same carbon footprint per kg as an average textile (21 vs 22 kg CO2e/kg), and it's fossil-based and sheds microfibres.
- **Leather ankle boots — 20 outfits, $150**
  - Unlocks 20 outfits, so its ~18.6 kg CO2e is spread over ~160 expected wears (~0.12 kg per wear).
  - Footwear is estimated per pair (~18.6 kg CO2e for typical boots in published LCAs); the material isn't modelled.

The spread works as intended. A redundant polyester cami scores **21 (E)**. A versatile linen piece that unlocks 42
outfits scores **93 (A)**. Cotton lands at **66 (B)** because WRAP puts cotton at 28 kg CO2e/kg, above the average
textile. Polyester doesn't beat cotton on carbon per kg in WRAP's data (21 vs 28), so the reasons never claim it does.
They mention its fossil origin and microfibres instead.

## Sanity checks

- Sandin et al. 2019 give 1-20 kg CO2e per garment life cycle (socks to jacket) as typical. Our garments fall in
  2-45 kg. The high end is heavy wool or acrylic. Sandin's 444 g jacket is ~20 kg; ours is 0.444 × 24 ≈ 10.7 kg in nylon.
- Sandin's per-use range is 0.04-0.7 kg CO2e per use. Our examples give 0.05-0.5 kg per wear.

## Limitations

- Material factors are generic world or UK averages. The actual factory's energy mix, dyeing and finishing (36% of
  apparel GHG per Quantis 2018) and transport aren't known.
- `fabric_guess` is Gemini's visual guess and is often missing on seeded items. Unknown material means the average textile.
- Durability isn't modelled. Laitala et al. 2018 show wool and silk are kept longer and washed less, so the wool coat's
  "E" is probably pessimistic. Cheap synthetics may wear out sooner.
- Microplastic shedding isn't in the CO2e number. It's only mentioned in the reasons.
- Shoe footprints come from single-product LCAs. Shoe material and water aren't modelled.
- Leather (for garments) and silk have wide published ranges. See the notes in the JSON.
- The utility curve, redundancy factors and score scale are transparent modelling choices, not measured behaviour.

## Sources

- WRAP (2012) *Valuing our clothes: the true cost of how we design, use and dispose of clothing in the UK*, Tables 1, 3 and 4. https://www.wrap.ngo/resources/report/valuing-our-clothes-true-cost-how-we-design-use-and-dispose-clothing-uk-2012 (PDF: https://www.fairact.org/wp-content/uploads/Wrap_Valuing_our_clothes_30pourcentsVoC_FINAL_online_2012_07_11.pdf)
- Ethical Consumer (2024) *What is the carbon cost of clothing?* (WRAP 2012 per-kg figures incl. silk, polyurethane, flax linen). https://www.ethicalconsumer.org/fashion-clothing/what-is-carbon-cost-clothing
- Nielsen & Schmidt / FORCE Technology (2014) *Changing consumer behaviour towards increased prevention of textile waste*, Nordic Council of Ministers NA2014:927, Table 3. https://norden.diva-portal.org/smash/get/diva2:764795/FULLTEXT01.pdf
- Textile Exchange / PE International (2014) *The Life Cycle Assessment of Organic Cotton Fiber*. https://textileexchange.org/app/uploads/2025/01/the-life-cycle-assessment-of-organic-cotton-fiber_38172.pdf
- Carbonfact (n.d., accessed 2026-09-26) *The Carbon Footprint of Polyester*. https://www.carbonfact.com/blog/knowledge/polyester-carbon-footprint
- Swiss FOEN (2017) *Recycled Textile Fibres and Textile Recycling*. https://www.bafu.admin.ch/dam/de/sd-web/BP6pNehIQR1W/Recycled-Textile-Fibres-and-Textile-Recycling.pdf
- Astudillo, Thalwitz & Vollrath (2015) *Life cycle assessment of silk production – a case study from India*, in Handbook of LCA of Textiles and Clothing, pp. 255-274 (values via Material Innovation Initiative: https://materialinnovation.org/wp-content/uploads/Silk-Report-Press-Release-.pdf)
- Brugnoli et al. (2025) *A global study on the LCA of the modern cow leather industry*, Discover Sustainability 6:80. https://doi.org/10.1007/s43621-025-00798-6. Range and LWG context via https://usleather.org/news-events/the-carbon-footprint-of-leather-a-critical-review-of-roncevich-et-al.-2026 (industry-affiliated)
- Leather Working Group / Spin360 (2024) *LCA summary*. https://www.leatherworkinggroup.com/fileadmin/user_upload/LWG_LCA_2024_Summary_.pdf
- Cheah, Ciceri, Olivetti, Matsumura, Forterre, Roth & Kirchain (2013) *Manufacturing-focused emissions reductions in footwear production*, J. Cleaner Production 44:18-29. https://dspace.mit.edu/bitstream/handle/1721.1/102070/Olivetti_Manufacturing-focused.pdf
- Bodoga, Nistorac, Loghin & Isopescu (2024) *Environmental Impact of Footwear Using LCA – Case Study of Professional Footwear*, Sustainability 16(14):6094. https://www.mdpi.com/2071-1050/16/14/6094
- Draft PEFCR Apparel & Footwear v1.1 (28 May 2021), Table 6 default number of wears. https://eeb.org/wp-content/uploads/2021/11/Draft-Product-Environmental-Footprint-Category-Rules-PEFCR-apparel-and-footwear.pdf
- Ecobalyse (French Ministry of Ecological Transition / ADEME), textile products.json and examples.json (accessed 2026-09-26). https://github.com/MTES-MCT/ecobalyse/tree/master/public/data/textile
- Sandin, Roos, Spak, Zamani & Peters (2019) *Environmental assessment of Swedish clothing consumption – six garments, sustainable futures*, Mistra Future Fashion. https://research.chalmers.se/publication/514322/file/514322_Fulltext.pdf
- Devine / Cotton Incorporated (2019) *Changes in Average Garment Weight & End-Use Demand*. https://www.cottonworks.com/wp-content/uploads/2019/09/Changes-in-Average-Garment-Weights_Jon-Devine.pdf
- Cooper et al. / WRAP (2014) *Clothing Longevity Protocol*, Figure 2. https://www.wrap.ngo/sites/default/files/2021-03/WRAP-clothing-longevity-protocol.pdf
- Ellen MacArthur Foundation (2017) *A New Textiles Economy – summary of findings*. https://content.ellenmacarthurfoundation.org/m/7f818b40f06e1afd/original/Summary-of-findings-A-New-Textiles-Economy.pdf
- Quantis (2018) *Measuring Fashion*. https://quantis.com/wp-content/uploads/2018/03/measuringfashion_globalimpactstudy_full-report_quantis_cwf_2018a.pdf
- Laitala, Klepp & Henry (2018) *Does Use Matter? Comparison of Environmental Impacts of Clothing Based on Fiber Type*, Sustainability 10(7):2524. https://www.mdpi.com/2071-1050/10/7/2524

### CREDITS.md section (ready to paste)

This section is now in the repo-root `CREDITS.md`:

```markdown
## Sustainability data
Fibre carbon/water factors: WRAP (2012) *Valuing our clothes* (Table 3), plus WRAP figures as reproduced by Ethical Consumer (2024) for linen, silk and polyurethane.
Organic cotton: Textile Exchange (2014). Recycled polyester: Carbonfact, Swiss FOEN (2017). Silk water: Astudillo et al. (2015). Leather: Brugnoli et al. (2025), LWG (2024).
Garment weights: Ecobalyse (French Ministry of Ecological Transition / ADEME), Sandin et al. (2019, Mistra Future Fashion), Cotton Incorporated (2019).
Default wears: draft PEFCR Apparel & Footwear v1.1 (2021). Footwear: Cheah et al. (2013, MIT), Bodoga et al. (2024).
Use-phase evidence: WRAP (2012, 2014), Ellen MacArthur Foundation (2017), Laitala et al. (2018), Quantis (2018). Full list: docs/SUSTAINABILITY.md.
```
