# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Offline, searchable sheet gallery. CAD strings are data, never HTML or code."""

import html
import json
import copy
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from .errors import CadError


def _asset(value):
    if not value:
        return ""
    path = PurePosixPath(str(value).replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or ":" in str(path):
        raise CadError("UNSAFE_PATH", "Report assets must use local relative paths.")
    return quote(str(path), safe="/")


def write_sheet_report(manifest, output_path):
    """Write one HTML gallery beside its manifest, DXF and SVG assets.

    No server, network requests or CAD calls are needed to inspect a sheet. The
    image viewer supports zoom/pan without injecting source SVG into the DOM.
    """
    sheets = []
    for index, item in enumerate(manifest.get("sheets", []), 1):
        preview = item.get("preview", item.get("render", {})) or {}
        notes = list(item.get("warnings", []))
        diagnostic_labels = {
            "EXTERNAL_CONTENT_PLACEHOLDER": "外部图片未嵌入，使用边框占位",
            "MISSING_GLYPHS": "部分字形缺失，须核对原始字体",
            "TEXT_RENDER_FAILED": "存在文字渲染失败",
            "TEXT_ENCODING_REPLACED": "存在无法解码的字符替代",
            "CUSTOM_CAD_TEXT_CODES": "专用 CAD 字符码需要原始字体核对",
            "ENTITY_SKIPPED": "存在未绘制的实体，详见诊断",
            "XREF_UNRESOLVED": "外部参照未解析",
            "HATCH_SIMPLIFIED": "高密度填充已按预览分辨率简化，DXF 保留原始图案",
        }
        for diagnostic in preview.get("diagnostics", []):
            if diagnostic.get("code") in diagnostic_labels:
                notes.append(diagnostic_labels[diagnostic["code"]])
        svg = item.get("svg") or item.get("files", {}).get("svg")
        dxf = item.get("dxf") or item.get("files", {}).get("dxf")
        if isinstance(svg, dict):
            svg = svg.get("path") or svg.get("output")
        if isinstance(dxf, dict):
            dxf = dxf.get("path") or dxf.get("output")
        thumbnail = item.get("thumbnail") or {}
        sheets.append({
            "id": item.get("id", str(index)),
            "name": item.get("title") or item.get("name") or f"图纸 {index}",
            "number": item.get("number") or item.get("drawing_number") or "",
            "file_name": item.get("name", ""),
            "svg": _asset(svg), "dxf": _asset(dxf),
            "thumbnail": _asset(thumbnail.get("output") or thumbnail.get("path")),
            "bbox": item.get("bbox", []),
            "entity_count": item.get("entity_count", 0),
            "warnings": notes,
            "preview": preview,
            "title_evidence": item.get("title_evidence", []),
            "confidence": item.get("confidence", item.get("title_confidence", "")),
        })
    data = {"sheets": sheets, "summary": manifest.get("summary", {}),
            "warnings": manifest.get("warnings", []),
            "source": manifest.get("source", {}),
            "statistics": manifest.get("statistics", {}),
            "verification": manifest.get("verification", {}),
            "feature_acceptance": copy.deepcopy(manifest.get("feature_acceptance", {}))}
    remainder = manifest.get("unassigned_artifact", {})
    if remainder:
        data["unassigned"] = {"dxf": _asset(remainder.get("dxf")),
                              "json": _asset(remainder.get("json")),
                              "entity_count": remainder.get("entity_count", 0)}
    conversion = data["source"].get("conversion", {}) if isinstance(data["source"], dict) else {}
    data["warnings"] = list(data["warnings"]) + list(conversion.get("warnings", []))
    column = data["feature_acceptance"].get("column", {})
    for key in ("svg", "png", "evidence"):
        if column.get(key):
            column[key] = _asset(column[key])
    encoded = json.dumps(data, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    encoded = encoded.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    source = manifest.get("source", {})
    source_name = Path(source.get("path", source.get("input", "CAD 图纸"))).name if isinstance(source, dict) else Path(str(source)).name
    page = _PAGE.replace("__SOURCE__", html.escape(source_name)).replace("__DATA__", encoded)
    target = Path(output_path)
    target.write_text(page, encoding="utf-8")
    return str(target)


_PAGE = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light"><title>TCAD · 图纸分图与预览</title>
<style>
:root{--ink:#172b3d;--muted:#657687;--line:#dce4eb;--blue:#176b81;--paper:#f4f7f9;--warn:#9a540b}*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.6 "Microsoft YaHei","Noto Sans CJK SC",system-ui,sans-serif}
header{background:#142c3e;color:#fff;padding:34px max(24px,5vw) 28px}header small{letter-spacing:3px;color:#92c5d4}h1{font-size:clamp(25px,3vw,38px);margin:8px 0}header p{margin:4px 0;color:#bdcfdb;overflow-wrap:anywhere}
main{max-width:1580px;margin:auto;padding:24px clamp(16px,4vw,64px) 64px}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}.stat{padding:18px 22px;background:white;border:1px solid var(--line);border-radius:8px}.stat b{display:block;font-size:28px;font-variant-numeric:tabular-nums}.stat span{color:var(--muted);font-size:13px}
.toolbar{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:24px 0 16px}input{flex:1;min-width:220px;padding:12px 16px;border:1px solid #b9cbd8;border-radius:6px;font:inherit}button,a.button{font:inherit;cursor:pointer;border:1px solid #bdccd6;border-radius:5px;padding:8px 12px;background:white;color:var(--ink);text-decoration:none}button:hover,a.button:hover{background:#e9f4f7;border-color:var(--blue)}button:focus-visible,a:focus-visible,input:focus-visible{outline:3px solid #58b3cf;outline-offset:2px}
.hint{color:var(--muted);font-size:13px}.gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:18px}.card{background:white;border:1px solid var(--line);border-radius:8px;overflow:hidden}.thumb{width:100%;height:226px;object-fit:contain;display:block;padding:10px;background:#fff;border-bottom:1px solid var(--line);cursor:zoom-in}.placeholder{height:226px;display:grid;place-content:center;color:var(--muted);background:#eef2f5}.card-body{padding:16px}.label{color:var(--blue);font-size:12px;font-weight:700;letter-spacing:.6px}.card h2{font-size:17px;margin:4px 0 10px;line-height:1.5}.meta{font-size:12px;color:var(--muted);margin-bottom:12px}.actions{display:flex;gap:8px;flex-wrap:wrap}.actions a,.actions button{font-size:13px}.warning{font-size:12px;color:var(--warn);margin-top:10px;overflow-wrap:anywhere}.notes{margin-top:28px;border:1px solid var(--line);border-radius:8px;background:white;padding:18px 22px}.notes h2{font-size:18px;margin:0 0 8px}.notes ul{padding-left:22px}.notes li{margin:5px 0;overflow-wrap:anywhere}code{font-size:12px;background:#edf3f6;padding:2px 5px;overflow-wrap:anywhere}details{margin-top:12px}summary{cursor:pointer}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;max-height:460px;overflow:auto}footer{margin-top:20px;color:var(--muted);font-size:12px}
dialog{width:96vw;max-width:1900px;height:94vh;max-height:1100px;padding:0;border:1px solid #afc0cd;border-radius:8px;color:var(--ink)}dialog::backdrop{background:#081d30bd}.viewer-bar{display:flex;align-items:center;gap:8px;padding:12px 16px;background:#f4f7f9;border-bottom:1px solid var(--line);flex-wrap:wrap}.viewer-title{font-size:15px;font-weight:600;flex:1;min-width:180px}.stage{height:calc(100% - 105px);overflow:hidden;position:relative;background:#e0e8ef;cursor:grab;touch-action:none}.stage:active{cursor:grabbing}.stage img{position:absolute;left:50%;top:50%;max-width:none;transform-origin:0 0;user-select:none;pointer-events:none;box-shadow:0 2px 15px #152a4120;background:white}.viewer-foot{padding:8px 16px;color:var(--muted);font-size:12px}.empty{padding:32px;text-align:center;color:var(--muted);grid-column:1/-1}
@media(max-width:680px){.stats{grid-template-columns:repeat(2,1fr)}header{padding:24px}.stat{padding:12px 16px}.stat b{font-size:23px}.gallery{grid-template-columns:1fr}.viewer-bar{padding:8px}.stage{height:calc(100% - 155px)}button{padding:7px}}
</style></head><body>
<header><small>TCAD AGENT CLI · SHEET WORKFLOW</small><h1>图纸分图与预览</h1><p>__SOURCE__</p><p>按图框拆分 · 读取图名命名 · 批量矢量预览 · 本地离线浏览</p></header>
<main><p id="acceptance" class="hint" hidden></p><section class="stats" aria-label="成果统计"><div class="stat"><b id="count">—</b><span>识别的图纸</span></div><div class="stat"><b id="svgCount">—</b><span>已生成 SVG</span></div><div class="stat"><b id="dxfCount">—</b><span>独立 DXF</span></div><div class="stat"><b id="warnCount">—</b><span>带提示的图纸</span></div></section>
<p class="hint" id="processing"></p><div class="toolbar"><input id="search" type="search" placeholder="搜索图名、图号或序号…" aria-label="搜索图纸"><a class="button" href="manifest.json" download>完整清单 JSON</a><a class="button" id="remainderLink" hidden>未归属实体 DXF</a><span class="hint" id="filtered"></span></div><p class="hint"><label><input id="onlyReview" type="checkbox" style="min-width:0"> 仅显示有缺字形诊断、需要核对原始字体的图纸</label></p>
<p class="hint"><a href="#features">查看柱图与 0.3.0 功能验收 ↓</a> · 点击图纸可放大、滚轮缩放和拖动；SVG 可单独打开。DXF 保留选中的原生实体及引用资源，跨图框实体的裁切范围见清单。此页面及图纸保存在同一目录，浏览无需再次调用 CLI。</p>
<section class="gallery" id="gallery" aria-label="图纸列表"></section>
<section class="notes" id="features" hidden><h2>0.3.0 实际图纸验收</h2><div id="featureContent"></div></section>
<section class="notes"><h2>验收与边界</h2><p>分图结果依据几何图框和文本规则生成。SVG 为二维预览，不用于量尺。缺失的外部参照、图片、字体或 DWG 转换不支持的对象须结合清单核对。</p><ul id="warnings"></ul><details><summary>查看来源、处理统计与验证记录</summary><pre id="audit"></pre></details></section>
<footer>作者 LiTao · TCAD Agent CLI · 本地生成，无外部脚本、字体或网络依赖</footer></main>
<dialog id="viewer"><div class="viewer-bar"><span class="viewer-title" id="viewerTitle"></span><button id="prev" aria-label="上一张">← 上一张</button><button id="next" aria-label="下一张">下一张 →</button><button id="zoomOut" aria-label="缩小">−</button><button id="fit">适合窗口</button><button id="zoomIn" aria-label="放大">＋</button><a class="button" id="openSvg" target="_blank" rel="noopener">原始 SVG ↗</a><button id="closeViewer">关闭 ×</button></div><div class="stage" id="stage"><img id="sheetImage" alt="图纸大图" draggable="false"></div><div class="viewer-foot" id="viewerFoot">滚轮缩放 · 拖动平移 · Esc 关闭 · ← → 切换图纸</div></dialog>
<script type="application/json" id="sheetData">__DATA__</script><script>
'use strict';
const data=JSON.parse(document.getElementById('sheetData').textContent),sheets=data.sheets,$=id=>document.getElementById(id);
const fmt=n=>Number(n||0).toLocaleString('zh-CN');
if(data.verification.reopened_dxf_count!==undefined){$('acceptance').hidden=false;$('acceptance').textContent=`实图验收：${data.verification.reopened_dxf_count} 张 DXF 已回读 · ${data.verification.parsed_svg_count} 张 SVG 已解析 · 图名内容独立核对 ${data.verification.matched_titles??data.verification.independent_title_content_set_matches??'见记录'} 张 · 源图${data.verification.source_unchanged?'未改动':'需核对'}。`}
const run=data.statistics.cli_resources;if(run)$('processing').textContent=`本次整批调用 ${Number(run.elapsed_seconds).toFixed(1)} 秒 · 峰值内存 ${(run.peak_rss_bytes/1048576).toFixed(1)} MiB · 全部实体细节保存在文件，命令返回摘要。`;
if(data.unassigned){$('remainderLink').hidden=false;$('remainderLink').href=data.unassigned.dxf;$('remainderLink').download='';$('remainderLink').textContent=`未归属实体 DXF（${data.unassigned.entity_count} 个）`}
$('count').textContent=fmt(sheets.length);$('svgCount').textContent=fmt(sheets.filter(s=>s.svg).length);$('dxfCount').textContent=fmt(sheets.filter(s=>s.dxf).length);$('warnCount').textContent=fmt(sheets.filter(s=>s.warnings.length).length);
function element(tag,text,cls){const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e}
function assetLink(label,url){const a=element('a',label,'button');a.href=url;a.target='_blank';a.rel='noopener';return a}
let filtered=sheets;function draw(){const q=$('search').value.toLowerCase().trim();filtered=sheets.filter(s=>[s.id,s.name,s.number,s.file_name].join(' ').toLowerCase().includes(q)&&(!$('onlyReview').checked||(s.preview.diagnostics||[]).some(d=>['MISSING_GLYPHS','TEXT_RENDER_FAILED','TEXT_ENCODING_REPLACED'].includes(d.code))));$('filtered').textContent=`显示 ${filtered.length} / ${sheets.length} 张`;$('gallery').replaceChildren();for(const s of filtered){const c=element('article',undefined,'card');if(s.svg){const img=element('img',undefined,'thumb');img.src=s.thumbnail||s.svg;img.loading='lazy';img.decoding='async';img.alt=s.name;img.tabIndex=0;img.onclick=()=>open(s);img.onkeydown=e=>{if(e.key==='Enter')open(s)};img.onerror=()=>{img.replaceWith(element('div','预览载入失败，请检查同目录 SVG 文件','placeholder'))};c.append(img)}else c.append(element('div','未生成预览，请检查清单中的错误','placeholder'));const b=element('div',undefined,'card-body');b.append(element('div',`${s.id}${s.number?' · '+s.number:''}`,'label'),element('h2',s.name),element('div',`${fmt(s.entity_count)} 个原生实体`,'meta'));const a=element('div',undefined,'actions');if(s.svg){const v=element('button','放大查看');v.onclick=()=>open(s);a.append(v,assetLink('SVG ↗',s.svg))}if(s.dxf){const d=assetLink('下载 DXF',s.dxf);d.download='';a.append(d)}b.append(a);if(s.warnings.length)b.append(element('div',`${s.warnings.length} 条处理提示 · 可展开下方记录`,'warning'));const details=element('details');details.append(element('summary','命名依据与显示诊断'),element('pre',JSON.stringify({bbox:s.bbox,title_evidence:s.title_evidence,confidence:s.confidence,warnings:s.warnings,font:s.preview.font,diagnostics:s.preview.diagnostics},null,2)));b.append(details);c.append(b);$('gallery').append(c)}if(!filtered.length)$('gallery').append(element('div','没有匹配的图纸','empty'))}
for(const warning of data.warnings)$('warnings').append(element('li',typeof warning==='string'?warning:JSON.stringify(warning)));
if(Object.keys(data.feature_acceptance).length){$('features').hidden=false;const f=data.feature_acceptance,c=f.column||{},body=$('featureContent');if(c.svg){body.append(element('h3',c.title||'柱图：轴网、轴号、柱子与标注'));const img=element('img');img.src=c.png||c.svg;img.alt='仅保留轴网、轴网标注、柱和柱标注的实际施工图';img.style.cssText='width:100%;max-height:740px;object-fit:contain;background:white';body.append(img);const actions=element('div',undefined,'actions');actions.append(assetLink('打开清晰柱图 SVG ↗',c.svg));if(c.evidence)actions.append(assetLink('柱图验证数据',c.evidence));body.append(actions,element('p',c.description||''));}for(const item of f.checks||[]){body.append(element('h3',item.name+' · '+item.status),element('p',item.detail));}const details=element('details');details.append(element('summary','命令、性能与未解决边界'),element('pre',JSON.stringify(f,null,2)));body.append(details);}
$('audit').textContent=JSON.stringify({source:data.source,summary:data.summary,statistics:data.statistics,verification:data.verification},null,2);$('search').addEventListener('input',draw);$('onlyReview').addEventListener('change',draw);draw();
let active=null,scale=1,x=0,y=0,drag=null,naturalW=1,naturalH=1;const image=$('sheetImage'),stage=$('stage'),dialog=$('viewer');
image.onerror=()=>{$('viewerFoot').textContent='图纸载入失败：请保留 HTML 与全部相邻 SVG/DXF 文件，或重新解压成果包。'};
function transform(){image.style.width=naturalW+'px';image.style.height=naturalH+'px';image.style.transform=`translate(${x}px,${y}px) scale(${scale})`}
function fit(){scale=Math.min((stage.clientWidth-32)/naturalW,(stage.clientHeight-32)/naturalH);x=-naturalW*scale/2;y=-naturalH*scale/2;transform()}
function zoom(factor,px=0,py=0){const next=Math.max(.01,Math.min(50,scale*factor)),r=next/scale;x=px-(px-x)*r;y=py-(py-y)*r;scale=next;transform()}
function open(s){if(!s.svg)return;active=s;$('viewerTitle').textContent=`${s.id} · ${s.name}`;$('openSvg').href=s.svg;$('viewerFoot').textContent=`${fmt(s.entity_count)} 个实体 · ${s.warnings.map(w=>typeof w==='string'?w:JSON.stringify(w)).join('；')||'滚轮缩放 · 拖动平移 · Esc 关闭 · ← → 切换图纸'}`;if(!dialog.open)dialog.showModal();image.onload=()=>{naturalW=image.naturalWidth||1600;naturalH=image.naturalHeight||1100;fit()};image.src=s.svg}
function step(d){const available=filtered.filter(s=>s.svg);if(!available.length)return;const i=available.indexOf(active);open(available[(i+d+available.length)%available.length])}
$('zoomIn').onclick=()=>zoom(1.4);$('zoomOut').onclick=()=>zoom(1/1.4);$('fit').onclick=fit;$('closeViewer').onclick=()=>dialog.close();$('prev').onclick=()=>step(-1);$('next').onclick=()=>step(1);stage.addEventListener('wheel',e=>{e.preventDefault();const r=stage.getBoundingClientRect();zoom(Math.exp(-e.deltaY*.0015),e.clientX-r.left-r.width/2,e.clientY-r.top-r.height/2)},{passive:false});stage.addEventListener('pointerdown',e=>{drag={px:e.clientX,py:e.clientY,x,y};stage.setPointerCapture(e.pointerId)});stage.addEventListener('pointermove',e=>{if(drag){x=drag.x+e.clientX-drag.px;y=drag.y+e.clientY-drag.py;transform()}});stage.addEventListener('pointerup',()=>drag=null);stage.addEventListener('pointercancel',()=>drag=null);dialog.addEventListener('keydown',e=>{if(e.key==='ArrowLeft')step(-1);if(e.key==='ArrowRight')step(1)});window.addEventListener('resize',()=>{if(dialog.open)fit()});
</script></body></html>
'''
