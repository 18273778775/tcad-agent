# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Build an offline room acceptance viewer from saved CAD and model evidence.

The displayed triangles are loaded from the published GLB, with every scene-node
transform applied.  Dimension tables quote the independently reopened-model
verification in summary.json; the viewer does not rebuild solids from the spec.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
from urllib.parse import quote
import xml.etree.ElementTree as ET

import numpy as np
import trimesh


AXES = ("length", "width", "height")


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def mesh_payload(path: Path) -> dict:
    """Read serialized vertices and faces, including world node transforms."""
    scene = trimesh.load_scene(path, process=False)
    meshes = []
    for node in sorted(scene.graph.nodes_geometry):
        transform, geometry_name = scene.graph[node]
        mesh = scene.geometry[geometry_name]
        if not isinstance(mesh, trimesh.Trimesh):
            raise ValueError(f"Non-triangular GLB geometry: {node}")
        vertices = trimesh.transform_points(np.asarray(mesh.vertices), transform)
        faces = np.asarray(mesh.faces, dtype=int)
        if not np.isfinite(vertices).all() or faces.ndim != 2 or faces.shape[1] != 3:
            raise ValueError(f"Invalid GLB mesh: {node}")
        if not len(vertices) or not len(faces):
            raise ValueError(f"Empty GLB mesh: {node}")
        meshes.append({"name": str(node), "vertices": vertices.tolist(),
                       "faces": faces.tolist()})
    if not meshes:
        raise ValueError(f"No meshes in {path}")
    vertices = np.concatenate([np.asarray(mesh["vertices"]) for mesh in meshes])
    return {"meshes": meshes,
            "bounds_m": [vertices.min(axis=0).tolist(), vertices.max(axis=0).tolist()],
            "component_count": len(meshes),
            "vertex_count": sum(len(mesh["vertices"]) for mesh in meshes),
            "triangle_count": sum(len(mesh["faces"]) for mesh in meshes),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def svg_payload(path: Path) -> str:
    """Inline the actual CLI preview, allowing no active or remote SVG content."""
    raw = path.read_text(encoding="utf-8")
    root = ET.fromstring(raw)
    if root.tag.rsplit("}", 1)[-1] != "svg":
        raise ValueError(f"Not an SVG document: {path}")
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1].lower() in {"script", "foreignobject", "image"}:
            raise ValueError(f"Active or external SVG content is unsupported: {path}")
        for attribute, value in element.attrib.items():
            key = attribute.rsplit("}", 1)[-1].lower()
            if key.startswith("on") or (key in {"href", "src"} and not value.startswith("#")):
                raise ValueError(f"Active or external SVG attribute is unsupported: {path}")
        if element.tag.rsplit("}", 1)[-1] == "style" and re.search(
                r"@import|url\s*\(", element.text or "", re.IGNORECASE):
            raise ValueError(f"External SVG styles are unsupported: {path}")
    return re.sub(r"^\s*<\?xml[^>]*\?>\s*", "", raw).strip()


def local_href(path: Path, output: Path) -> str:
    return quote(Path(os.path.relpath(path, output.parent)).as_posix(), safe="/.")


def build_payload(run_dir: Path, output: Path) -> dict:
    summary = read_json(run_dir / "summary.json")
    states = {}
    for state, spec_name, preview_name in (
            ("before", "before-dwg.room.json", "before-dwg.svg"),
            ("after", "after.room.json", "after.svg")):
        spec = read_json(run_dir / spec_name)
        model = run_dir / "models" / f"{state}.glb"
        verification = summary[state]["models"]["glb"]["verification"]
        actual = mesh_payload(model)
        if actual["sha256"] != summary[state]["models"]["glb"]["sha256"]:
            raise ValueError(f"GLB hash differs from the saved acceptance evidence: {model}")
        for axis in AXES:
            expected_mm = spec["dimensions"][axis] * spec["drawing_unit_to_m"] * 1000
            saved_mm = verification["dimensions"][axis]["expected_mm"]
            if not math.isclose(expected_mm, saved_mm, rel_tol=1e-12, abs_tol=1e-9):
                raise ValueError(f"Spec and saved verification disagree: {state}/{axis}")
        if actual["component_count"] != verification["components"]["count"]:
            raise ValueError(f"GLB component count differs from verification: {state}")
        states[state] = {"spec": spec, "verification": verification, "model": actual,
                         "preview_svg": svg_payload(run_dir / "previews" / preview_name),
                         "model_links": {
                             extension: local_href(run_dir / "models" / f"{state}.{extension}",
                                                   output)
                             for extension in ("glb", "obj", "dxf")},
                         "spec_link": local_href(run_dir / spec_name, output),
                         "preview_link": local_href(run_dir / "previews" / preview_name, output)}
    manifest = []
    candidates = [
        ("源 DWG", "fixtures/controlled-complete-room.dwg"),
        ("源 DXF", "fixtures/controlled-complete-room.dxf"),
        ("修改后的 DXF", "revised-complete-room.dxf"),
        ("原子编辑计划", "plans/revise-length-width-height.json"),
        ("图层与语义映射", "fixtures/room-map.json"),
        ("DWG 来源记录", "fixtures/dwg-provenance.json"),
        ("验收汇总 JSON", "summary.json"),
        ("完整 CLI 请求与响应清单", "commands.json"),
    ]
    for label, relative in candidates:
        path = run_dir / relative
        if path.is_file():
            manifest.append({"label": label, "path": relative,
                             "href": local_href(path, output)})
    return {"run_name": run_dir.name, "summary": summary, "states": states,
            "manifest": manifest,
            "provenance": "受控完整房间验收样例；不是来自独立建筑项目的施工图。",
            "renderer": "实际 GLB 三角网格；已应用场景节点世界变换；本地离线绘制。"}


HTML = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light">
<title>TCAD · 完整房间验收</title>
<style>
:root{--paper:#f6f5f0;--panel:#fffefa;--ink:#213734;--muted:#697873;--line:#dce2db;--teal:#286d60;--mint:#e4eee6;--amber:#a96d22;--mono:Consolas,"Cascadia Mono",monospace}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:14px/1.65 "Segoe UI","Microsoft YaHei",sans-serif}button,input,select{font:inherit}button,a,input{ -webkit-tap-highlight-color:transparent}button{cursor:pointer}a{color:var(--teal);text-decoration:none}a:hover{text-decoration:underline}a:focus-visible,button:focus-visible,input:focus-visible,canvas:focus-visible{outline:3px solid #a7cbbb;outline-offset:3px}main{max-width:1550px;margin:auto;padding:0 48px 36px}.topbar{height:73px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--line)}.brand{display:flex;align-items:center;gap:11px;font-weight:650;letter-spacing:.13em}.brandmark{display:grid;place-items:center;width:28px;height:28px;background:var(--teal);color:var(--paper);font-size:12px;border-radius:4px}.topnote{display:flex;gap:22px;align-items:center;font-size:12px;color:var(--muted)}.badge{border:1px solid #c6d9cc;border-radius:20px;padding:3px 11px;background:#edf5ed;color:#34664a;font-size:12px}.badge::before{content:"";display:inline-block;width:6px;height:6px;border-radius:50%;background:#579065;margin-right:7px}.badge.fail{color:#9e402b;border-color:#e7c5b8;background:#fff0e8}.badge.fail::before{background:#a94429}.intro{display:flex;justify-content:space-between;align-items:flex-end;padding:32px 0 25px;gap:24px}.eyebrow{color:var(--teal);font-size:11px;font-weight:650;letter-spacing:.2em;margin:0 0 6px}.intro h1{font-size:31px;font-weight:550;letter-spacing:.01em;margin:0 0 7px;line-height:1.4}.intro p{margin:0;color:var(--muted);font-size:13px}.run{font:11px var(--mono);color:var(--muted);white-space:nowrap;padding-bottom:3px}.flow{display:flex;flex-wrap:wrap;gap:9px 15px;margin:0 0 26px;padding:0;list-style:none;color:var(--muted);font-size:11px}.flow li{display:flex;align-items:center;gap:8px}.flow b{font:11px var(--mono);color:var(--teal)}.flow li:not(:last-child)::after{content:"→";margin-left:7px;color:#aab6ac}.workspace{display:grid;grid-template-columns:minmax(0,1.64fr) minmax(360px,1fr);gap:20px;align-items:start}.card{background:var(--panel);border:1px solid var(--line);border-radius:11px;overflow:hidden}.cardhead{display:flex;align-items:center;justify-content:space-between;padding:16px 20px;border-bottom:1px solid var(--line);gap:10px}.cardhead h2{font-size:13px;font-weight:650;margin:0;letter-spacing:.02em}.tiny{font-size:10px;color:var(--muted)}.segments{display:flex;background:#e9ede6;padding:3px;gap:3px;border-radius:6px}.segments button{font-size:11px;border:0;color:var(--muted);padding:5px 14px;background:none;border-radius:4px}.segments button.active{background:var(--panel);color:var(--ink);box-shadow:0 1px 3px #21373417}.stage{height:420px;position:relative;background:radial-gradient(ellipse at 50% 45%,#f3f4ec 0%,#e8ede4 100%);overflow:hidden}.stage canvas{position:absolute;left:0;top:0;width:100%;height:100%;touch-action:none}.stage #overlay{pointer-events:none}.scene-label{position:absolute;top:17px;left:19px;z-index:2;pointer-events:none;font-size:10px;color:#62776a;letter-spacing:.08em}.scene-label b{display:block;color:#2f5144;font:600 12px var(--mono);margin-bottom:2px}.scenehint{position:absolute;bottom:14px;left:19px;font-size:10px;color:#687d6e;pointer-events:none}.stage-controls{padding:12px 18px;display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;border-top:1px solid var(--line)}.toggles{display:flex;gap:14px;flex-wrap:wrap}.toggles label{display:flex;align-items:center;gap:5px;font-size:11px;color:#57665e;cursor:pointer}.toggles input{accent-color:var(--teal);width:12px;height:12px;margin:0}.smallbtn{border:1px solid var(--line);padding:4px 9px;background:#fafbf7;color:#526859;border-radius:4px;font-size:10px}.scenefoot{display:flex;justify-content:space-between;gap:12px;padding:11px 18px;background:#fafbf7;border-top:1px solid var(--line);font-size:10px;color:var(--muted)}.scenefoot span:first-child{font-family:var(--mono)}.metricgrid{display:grid;grid-template-columns:repeat(3,1fr);padding:19px 20px 17px;gap:12px}.metric .label{font-size:10px;color:var(--muted)}.metric strong{display:block;font:500 25px/1.5 var(--mono);letter-spacing:-.06em}.metric em{font-style:normal;font-size:10px;color:var(--muted)}.metric .delta{color:var(--teal);font-size:10px}.tablewrap{padding:0 20px}table{border-collapse:collapse;width:100%;font-size:11px}th{font-size:10px;font-weight:500;color:var(--muted);background:#f5f7f1}th,td{text-align:right;padding:9px 7px;border-bottom:1px solid #e8ebe3;white-space:nowrap}th:first-child,td:first-child{text-align:left;padding-left:0}td{font-family:var(--mono);color:#3f5148}td.label{font-family:inherit;color:var(--ink)}.error{color:var(--teal)}.comparison-note{margin:11px 20px 0;color:var(--muted);font-size:10px}.construction{padding:17px 20px 16px;display:grid;grid-template-columns:1fr 1fr;gap:9px 16px}.construction span{display:flex;justify-content:space-between;gap:8px;font-size:11px}.construction i{color:var(--muted);font-style:normal}.construction b{font:11px var(--mono);font-weight:400}.verification-summary{margin:0 20px 20px;background:#edf4eb;border:1px solid #d8e5d4;border-radius:6px;padding:11px 13px;display:flex;justify-content:space-between;align-items:center;gap:10px}.verification-summary strong{font-size:11px;font-weight:600}.verification-summary small{display:block;color:var(--muted);font-size:10px}.verification-summary .value{font:16px var(--mono);color:var(--teal)}.lower{margin-top:20px;display:grid;grid-template-columns:minmax(0,1.64fr) minmax(360px,1fr);gap:20px}.planstage{height:255px;display:flex;align-items:center;justify-content:center;background:#212830;padding:12px;overflow:hidden}.planstage svg{display:block;width:100%;height:100%;max-width:100%;max-height:100%}.planfoot{font-size:10px;color:var(--muted);padding:11px 18px;background:#fafbf7}.opening-list{padding:0 20px}.opening{padding:15px 0;border-bottom:1px solid #e5e9e0}.opening:last-child{border-bottom:0}.openinghead{display:flex;align-items:center;justify-content:space-between;font-size:12px;margin-bottom:7px}.openinghead b{font-weight:550}.openinghead span{font-size:10px;color:var(--teal)}.openingvalues{display:grid;grid-template-columns:1fr 1fr;gap:5px 16px;font-size:10px;color:var(--muted)}.openingvalues b{font:10px var(--mono);color:var(--ink);font-weight:400}.openingnote{padding:9px 20px 16px;font-size:10px;color:var(--muted)}.audit{margin-top:20px;display:grid;grid-template-columns:1fr 1fr;gap:20px}.auditcontent{padding:14px 20px 20px}.checks{display:grid;grid-template-columns:1fr 1fr;gap:8px 18px;font-size:11px;color:#53675b}.checks span::before{content:"✓";color:var(--teal);margin-right:7px}.checks span.fail::before{content:"×";color:#a94429}.format-table{margin-top:14px}.format-table td,.format-table th{font-size:10px;padding-top:6px;padding-bottom:6px}.artifacts{display:grid;grid-template-columns:1fr 1fr;gap:8px 18px;margin:0;padding:0;list-style:none}.artifacts a{display:block;border-bottom:1px solid #e7ebe2;padding:5px 0;font-size:11px}.artifacts a::after{content:" ↗";font-size:10px;opacity:.55}.downloadrow{display:flex;gap:15px;margin-top:16px;padding-top:12px;border-top:1px solid var(--line);font-size:10px}.scope{border-top:1px solid var(--line);margin:25px 0 0;padding-top:15px;font-size:10px;color:var(--muted);display:grid;grid-template-columns:1.2fr 1fr;gap:40px}.scope p{margin:0 0 5px}.scope strong{color:#506354;font-weight:600}.noscript{padding:24px;color:#7e412f}.failnote{color:#9e402b}.cut-note{position:absolute;right:17px;bottom:14px;color:#687d6e;font-size:10px;pointer-events:none}
@media(min-width:1500px){.stage{height:445px}}@media(max-width:1050px){main{padding:0 22px 28px}.workspace,.lower{grid-template-columns:1.35fr 1fr;gap:14px}.intro h1{font-size:26px}.metric strong{font-size:22px}.tablewrap{padding:0 13px}th,td{padding-left:4px;padding-right:4px}.stage{height:405px}.construction{padding-left:15px;padding-right:15px}.audit{gap:14px}.topnote{gap:10px}}@media(max-width:800px){.workspace,.lower,.audit{grid-template-columns:1fr}.topnote .tiny{display:none}.intro{padding-top:24px}.intro h1{font-size:25px}.run{display:none}.stage{height:410px}.lower,.audit{margin-top:14px}.scope{grid-template-columns:1fr;gap:10px}.planstage{height:250px}}@media print{main{padding:0}.stage{height:350px}.toggles,.smallbtn,.scenehint{display:none}.card{break-inside:avoid}.workspace,.lower{grid-template-columns:1.4fr 1fr}.audit{grid-template-columns:1fr 1fr}.scope{font-size:9px}.intro{padding-top:18px}a{color:inherit}}
</style>
</head>
<body>
<main>
<header class="topbar"><div class="brand"><span class="brandmark">T</span>TCAD <span class="tiny">/ 验收记录</span></div><div class="topnote"><span class="tiny">CAD → 三维重建 → 独立复测</span><span class="badge" id="overallBadge">读取验收记录</span></div></header>
<section class="intro"><div><div class="eyebrow">CONTROLLED ROOM · EVIDENCE VIEWER</div><h1>完整房间，图纸与三维逐项对照</h1><p id="introText">读取真实模型与验收证据。</p></div><span class="run" id="runName"></span></section>
<ol class="flow"><li><b>01</b>读取 DWG / DXF</li><li><b>02</b>核验长、宽、净高</li><li><b>03</b>原子修改并重建实体</li><li><b>04</b>重新打开三种格式独立复测</li></ol>
<section class="workspace">
<article class="card modelcard"><div class="cardhead"><h2>三维实体</h2><div class="segments" role="group" aria-label="切换修改前后"><button data-state="before">修改前</button><button data-state="after" class="active">修改后</button></div></div><div class="stage" id="stage"><canvas id="scene" tabindex="0" aria-label="实际房间三维模型，拖动旋转，滚轮缩放，方向键调整视角"></canvas><canvas id="overlay" aria-hidden="true"></canvas><div class="scene-label"><b id="sceneState"></b><span>实际 GLB 网格 · Z 轴向上</span></div><div class="scenehint">拖动旋转 · 滚轮缩放 · 方向键微调</div><div class="cut-note" id="cutNote"></div></div><div class="stage-controls"><div class="toggles"><label><input id="ceiling" type="checkbox">显示顶板</label><label><input id="southCut" type="checkbox">南墙剖切</label><label><input id="eastCut" type="checkbox" checked>东墙剖切</label><label><input id="guides" type="checkbox" checked>尺寸 / 门窗</label></div><button id="resetView" class="smallbtn">复位视角</button></div><div class="scenefoot"><span id="meshStats"></span><span>剖切仅影响显示；文件包含全部构件</span></div></article>
<article class="card dimensions"><div class="cardhead"><h2>尺寸与重开验证</h2><span class="tiny">全部换算为 mm</span></div><div class="metricgrid" id="dimensionMetrics"></div><div class="tablewrap"><table><thead><tr><th>净尺寸</th><th>CAD 几何</th><th>重开 GLB</th><th>绝对误差</th></tr></thead><tbody id="dimensionRows"></tbody></table></div><p class="comparison-note">长、宽取墙内表面；净高取完成地面至顶板下表面。外包尺寸另列。</p><div class="construction" id="construction"></div><div class="verification-summary"><div><strong id="verificationTitle"></strong><small id="verificationNote"></small></div><span class="value" id="tolerance"></span></div></article>
</section>
<section class="lower"><article class="card"><div class="cardhead"><h2>原始 CAD 预览</h2><a id="previewLink" class="tiny" href="#">打开原始 SVG ↗</a></div><div class="planstage" id="plan"></div><div class="planfoot" id="planFoot"></div></article><article class="card"><div class="cardhead"><h2>真实门窗洞口</h2><span class="tiny">根据实体间隙复测</span></div><div class="opening-list" id="openings"></div><div class="openingnote">模型保留穿透墙厚的空洞。门扇、玻璃等示意线仅在原 CAD 中，不作为实体填入洞口。</div></article></section>
<section class="audit"><article class="card"><div class="cardhead"><h2>验收检查</h2><span class="tiny">来自 summary.json</span></div><div class="auditcontent"><div class="checks" id="checks"></div><table class="format-table"><thead><tr><th>格式 / 状态</th><th>净尺寸最大误差 mm</th><th>门窗空洞</th><th>重开验证</th></tr></thead><tbody id="formats"></tbody></table></div></article><article class="card"><div class="cardhead"><h2>源文件与复核材料</h2><span class="tiny">本地相对路径</span></div><div class="auditcontent"><ul class="artifacts" id="manifest"></ul><div class="downloadrow" id="downloads"></div><div class="tiny" id="hashNote" style="margin-top:10px;overflow-wrap:anywhere"></div></div></article></section>
<footer class="scope"><div><p><strong>证据范围</strong></p><p id="scopeText"></p><p>当前方法支持具名、封闭、轴对齐的矩形构件。各构件独立闭合；未声称整体经过布尔融合，亦未进行结构设计或施工合规审查。</p></div><div><p><strong>显示与测量</strong></p><p id="renderText"></p><p>三维显示用于空间检查；数值判定采用已保存的序列化模型复测结果。页面无网络依赖，离线打开即可旋转查看。</p></div></footer>
<noscript><p class="noscript">此查看器需要 JavaScript 显示嵌入的模型与验收数据。源文件和完整验收证据保存在同一目录。</p></noscript>
</main>
<script type="application/json" id="evidence">__EVIDENCE_JSON__</script>
<script>
"use strict";
const data=JSON.parse(document.getElementById("evidence").textContent);
const $=id=>document.getElementById(id),esc=value=>String(value).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const axisNames={length:"净长 L",width:"净宽 W",height:"净高 H"},sideNames={south:"南墙",north:"北墙",east:"东墙",west:"西墙"};
const fmt=(n,d=2)=>Number(n).toLocaleString("zh-CN",{maximumFractionDigits:d});
const exact=n=>Number(n).toPrecision(12),err=n=>n===0?"0":Number(n).toFixed(6).replace(/0+$/,"").replace(/\.$/,"");
let current="after",az=-1.02,elev=.65,zoom=1,drag=null,width=1,height=1;
const scene=$("scene"),overlay=$("overlay"),ctx=overlay.getContext("2d"),stage=$("stage");
const gl=scene.getContext("webgl",{antialias:true,alpha:true,preserveDrawingBuffer:true});
let program,positionBuffer,colorBuffer,pLoc,cLoc;
if(gl){
 const shader=(type,source)=>{const s=gl.createShader(type);gl.shaderSource(s,source);gl.compileShader(s);if(!gl.getShaderParameter(s,gl.COMPILE_STATUS))throw Error(gl.getShaderInfoLog(s));return s};
 program=gl.createProgram();gl.attachShader(program,shader(gl.VERTEX_SHADER,"attribute vec3 position;attribute vec4 color;varying vec4 vColor;void main(){gl_Position=vec4(position,1.0);vColor=color;}"));gl.attachShader(program,shader(gl.FRAGMENT_SHADER,"precision mediump float;varying vec4 vColor;void main(){gl_FragColor=vColor;}"));gl.linkProgram(program);gl.useProgram(program);pLoc=gl.getAttribLocation(program,"position");cLoc=gl.getAttribLocation(program,"color");positionBuffer=gl.createBuffer();colorBuffer=gl.createBuffer();gl.enable(gl.DEPTH_TEST);gl.depthFunc(gl.LEQUAL);gl.enable(gl.BLEND);gl.blendFunc(gl.SRC_ALPHA,gl.ONE_MINUS_SRC_ALPHA);
}
const sub=(a,b)=>a.map((v,i)=>v-b[i]),dot=(a,b)=>a.reduce((s,v,i)=>s+v*b[i],0),cross=(a,b)=>[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]],norm=a=>{const m=Math.hypot(...a);return a.map(v=>v/m)};
function update(){
 const item=data.states[current],spec=item.spec,v=item.verification,other=data.states.before.spec,mm=spec.drawing_unit_to_m*1000;
 document.querySelectorAll("[data-state]").forEach(b=>{b.classList.toggle("active",b.dataset.state===current);b.setAttribute("aria-pressed",b.dataset.state===current)});
 $("sceneState").textContent=`${spec.name} / ${current==="after"?"修改后":"修改前"}`;
 $("meshStats").textContent=`${item.model.component_count} 构件 · ${item.model.vertex_count} 顶点 · ${item.model.triangle_count} 三角形 · m`;
 $("dimensionMetrics").innerHTML=Object.keys(axisNames).map(axis=>{const value=spec.dimensions[axis]*mm,delta=value-other.dimensions[axis]*other.drawing_unit_to_m*1000;return `<div class="metric"><div class="label">${axisNames[axis]}</div><strong>${fmt(value,3)}</strong><em>mm</em><div class="delta">${current==="before"?"原始几何":(delta>=0?"+":"")+fmt(delta,3)+" mm 修改量"}</div></div>`}).join("");
 $("dimensionRows").innerHTML=Object.keys(axisNames).map(axis=>{const d=v.dimensions[axis];return `<tr><td class="label">${axisNames[axis]}</td><td title="${exact(d.expected_mm)}">${fmt(d.expected_mm,4)}</td><td title="${exact(d.actual_mm)}">${fmt(d.actual_mm,4)}</td><td class="error" title="${exact(d.absolute_error_mm)}">${err(d.absolute_error_mm)}</td></tr>`}).join("");
 const outer=item.model.bounds_m[1].map((n,i)=>(n-item.model.bounds_m[0][i])*1000),construction=[["墙厚",fmt(spec.construction.wall_thickness*mm,3)+" mm"],["地板 / 顶板",fmt(spec.construction.floor_thickness*mm,3)+" / "+fmt(spec.construction.ceiling_thickness*mm,3)+" mm"],["净地面积",fmt(spec.dimensions.length*spec.dimensions.width*spec.drawing_unit_to_m**2,3)+" m²"],["模型外包 L × W × H",outer.map(n=>fmt(n,3)).join(" × ")+" mm"]];
 $("construction").innerHTML=construction.map(([label,value])=>`<span${label.startsWith("模型")?' style="grid-column:1/-1"':""}><i>${label}</i><b>${value}</b></span>`).join("");
 $("verificationTitle").textContent=v.verified&&v.passed?"三维实体复测通过":"三维实体复测未通过";
 $("verificationNote").textContent=`图纸单位 ${spec.units} · GLB 坐标单位 ${v.model_units} · 逐项绝对误差 ≤ 允许值`;
 $("tolerance").textContent=`${fmt(v.tolerance_mm,6)} mm`;
 $("plan").innerHTML=item.preview_svg;$("previewLink").href=item.preview_link;
 $("planFoot").textContent=current==="before"?"源 DWG 经 CLI 转换后渲染的完整图纸；包括平面、净高与门窗立面证据。":"修改后的 DXF 原生渲染；完整保留平面、净高与门窗立面证据。";
 $("openings").innerHTML=v.openings.map(o=>{const m=o.measurements,max=Math.max(...Object.values(m).map(a=>a.absolute_error_mm));return `<div class="opening"><div class="openinghead"><b>${esc(o.id)} · ${o.kind==="door"?"门洞":"窗洞"} / ${esc(sideNames[o.wall]||o.wall)}</b><span>${o.passed&&o.void_clear?"✓ 空洞贯通":"未通过"}</span></div><div class="openingvalues"><span>CAD 宽 × 高<br><b>${fmt(m.width.expected_mm,3)} × ${fmt(m.height.expected_mm,3)} mm</b></span><span>重开模型 宽 × 高<br><b>${fmt(m.width.actual_mm,4)} × ${fmt(m.height.actual_mm,4)} mm</b></span><span>洞口偏移 / 下沿标高<br><b>${fmt(m.offset.actual_mm,4)} / ${fmt(m.sill.actual_mm,4)} mm</b></span><span>四项最大误差<br><b>${err(max)} mm</b></span></div></div>`}).join("");
 $("formats").innerHTML=["before","after"].flatMap(state=>Object.entries(data.summary[state].models).map(([format,m])=>{const verification=m.verification,max=Math.max(...Object.values(verification.dimensions).map(d=>d.absolute_error_mm));return `<tr><td class="label">${format.toUpperCase()} / ${state==="before"?"修改前":"修改后"}</td><td title="${exact(max)}">${err(max)}</td><td>${verification.openings.filter(o=>o.void_clear&&o.passed).length}/${verification.openings.length}</td><td class="error">${verification.verified&&verification.passed?"通过":"失败"}</td></tr>`})).join("");
 $("downloads").innerHTML=[...Object.entries(item.model_links).map(([format,href])=>`<a href="${esc(href)}" download>下载 ${format==="dxf"?"3D DXF":format.toUpperCase()}</a>`),`<a href="${esc(item.spec_link)}">房间语义 JSON</a>`].join("");
 $("hashNote").textContent=`当前 GLB SHA-256：${item.model.sha256}`;
 draw();
}
function camera(){
 const model=data.states[current].model,bounds=model.bounds_m,center=bounds[0].map((n,i)=>(n+bounds[1][i])/2),radius=Math.hypot(...sub(bounds[1],bounds[0]))*.5;
 const direction=[Math.cos(az)*Math.cos(elev),Math.sin(az)*Math.cos(elev),Math.sin(elev)],right=[-Math.sin(az),Math.cos(az),0],up=cross(direction,right),scale=Math.min(width,height)*.67/radius*zoom;
 return {center,radius,direction,right,up,scale};
}
function projection(v,c){const p=sub(v,c.center);return [width*.5+dot(p,c.right)*c.scale,height*.5-dot(p,c.up)*c.scale,-dot(p,c.direction)/(c.radius*4)]}
function clip(v,c){const p=projection(v,c);return [p[0]/width*2-1,1-p[1]/height*2,p[2]]}
function visible(mesh){return !(mesh.name==="tcad_ceiling"&&!$("ceiling").checked)&&!(mesh.name.startsWith("tcad_wall_south_")&&$("southCut").checked)&&!(mesh.name.startsWith("tcad_wall_east_")&&$("eastCut").checked)}
function drawBuffer(positions,colors,mode){if(!positions.length)return;gl.bindBuffer(gl.ARRAY_BUFFER,positionBuffer);gl.bufferData(gl.ARRAY_BUFFER,new Float32Array(positions),gl.DYNAMIC_DRAW);gl.enableVertexAttribArray(pLoc);gl.vertexAttribPointer(pLoc,3,gl.FLOAT,false,0,0);gl.bindBuffer(gl.ARRAY_BUFFER,colorBuffer);gl.bufferData(gl.ARRAY_BUFFER,new Float32Array(colors),gl.DYNAMIC_DRAW);gl.enableVertexAttribArray(cLoc);gl.vertexAttribPointer(cLoc,4,gl.FLOAT,false,0,0);gl.drawArrays(mode,0,positions.length/3)}
function draw(){
 if(!width||!height)return;const c=camera(),item=data.states[current],v=item.verification;
 ctx.clearRect(0,0,width,height);
 if(gl){
  gl.viewport(0,0,scene.width,scene.height);gl.clearColor(0,0,0,0);gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);gl.depthMask(true);
  const p=[],col=[],edges=[],edgeColors=[],transparent=[],tcol=[],light=norm([-.4,-.6,1]);
  for(const mesh of item.model.meshes){if(!visible(mesh))continue;const ceiling=mesh.name==="tcad_ceiling",base=mesh.name==="tcad_floor"?[.63,.59,.50]:ceiling?[.72,.79,.75]:mesh.name.includes("north")?[.66,.76,.69]:[.78,.81,.74],edgeMap=new Map();
   for(const face of mesh.faces){const points=face.map(i=>mesh.vertices[i]),n=norm(cross(sub(points[1],points[0]),sub(points[2],points[0]))),shade=.69+Math.max(0,dot(n,light))*.3;for(const point of points){(ceiling?transparent:p).push(...clip(point,c));(ceiling?tcol:col).push(...base.map(a=>a*shade),ceiling?.48:1)}for(let j=0;j<3;j++){const a=face[j],b=face[(j+1)%3],key=[a,b].sort((x,y)=>x-y).join(":");if(!edgeMap.has(key))edgeMap.set(key,{a,b,normals:[]});edgeMap.get(key).normals.push(n)}}
   if(!ceiling)for(const e of edgeMap.values()){if(e.normals.length>1&&Math.abs(dot(e.normals[0],e.normals[1]))>.999)continue;for(const i of [e.a,e.b]){const q=clip(mesh.vertices[i],c);q[2]-=.00007;edges.push(...q);edgeColors.push(.26,.34,.28,.48)}}
  }
  gl.enable(gl.POLYGON_OFFSET_FILL);gl.polygonOffset(1,1);drawBuffer(p,col,gl.TRIANGLES);gl.disable(gl.POLYGON_OFFSET_FILL);drawBuffer(edges,edgeColors,gl.LINES);gl.depthMask(false);drawBuffer(transparent,tcol,gl.TRIANGLES);gl.depthMask(true);
 }else{
  ctx.fillStyle="#7a4c39";ctx.font='13px "Microsoft YaHei",sans-serif';ctx.fillText("当前浏览器未提供 WebGL；请使用支持 WebGL 的浏览器打开同一文件。",20,height/2);
 }
 // The annotations use reopened measurements, never guessed screen extents.
 if($("guides").checked){
  const L=v.dimensions.length.actual_mm/1000,W=v.dimensions.width.actual_mm/1000,H=v.dimensions.height.actual_mm/1000,t=item.spec.construction.wall_thickness*item.spec.drawing_unit_to_m,pad=Math.max(L,W)*.07;
  dimension([0,-t-pad,0],[L,-t-pad,0],`L  ${fmt(v.dimensions.length.actual_mm,3)} mm`,c);
  dimension([L+t+pad,0,0],[L+t+pad,W,0],`W  ${fmt(v.dimensions.width.actual_mm,3)} mm`,c);
  dimension([-t-pad,W+t,0],[-t-pad,W+t,H],`H  ${fmt(v.dimensions.height.actual_mm,3)} mm`,c);
  for(const o of v.openings){const m=o.measurements,scale=v.model_unit_to_m,a=m.offset.actual_model_units*scale,b=a+m.width.actual_model_units*scale,z=m.sill.actual_model_units*scale,h=m.height.actual_model_units*scale,alongX=o.wall==="south"||o.wall==="north",fixed={south:-t/2,north:W+t/2,west:-t/2,east:L+t/2}[o.wall],point=alongX?[(a+b)/2,fixed,z+h/2]:[fixed,(a+b)/2,z+h/2];marker(point,`${o.id} ${o.kind==="door"?"门洞":"窗洞"}`,c)}
 }
 axisGizmo(c);$("cutNote").textContent=[$("southCut").checked?"南墙":"",$("eastCut").checked?"东墙":""].filter(Boolean).join(" / ")?"剖切："+[$("southCut").checked?"南墙":"",$("eastCut").checked?"东墙":""].filter(Boolean).join(" / "):"完整墙体";
}
function line(points,c,color="#7d9380",dash=[]){ctx.strokeStyle=color;ctx.lineWidth=1;ctx.setLineDash(dash);ctx.beginPath();points.forEach((p,i)=>{const q=projection(p,c);i?ctx.lineTo(q[0],q[1]):ctx.moveTo(q[0],q[1])});ctx.stroke();ctx.setLineDash([])}
function label(text,x,y,color="#42694e"){ctx.font='11px "Segoe UI","Microsoft YaHei",sans-serif';ctx.textAlign="center";ctx.textBaseline="middle";const w=ctx.measureText(text).width+14;ctx.fillStyle="rgba(249,250,243,.92)";ctx.fillRect(x-w/2,y-10,w,20);ctx.fillStyle=color;ctx.fillText(text,x,y)}
function dimension(a,b,text,c){line([a,b],c,"#7d9380",[4,4]);const p=projection(a,c),q=projection(b,c);ctx.strokeStyle="#7d9380";ctx.lineWidth=1;const dx=q[0]-p[0],dy=q[1]-p[1],len=Math.hypot(dx,dy),nx=-dy/len*4,ny=dx/len*4;for(const point of [p,q]){ctx.beginPath();ctx.moveTo(point[0]-nx,point[1]-ny);ctx.lineTo(point[0]+nx,point[1]+ny);ctx.stroke()}label(text,(p[0]+q[0])/2,(p[1]+q[1])/2,"#42694e")}
function marker(point,text,c){const p=projection(point,c),offset=point[1]>c.center[1]?-25:23;ctx.fillStyle="#b47d3d";ctx.beginPath();ctx.arc(p[0],p[1],3,0,Math.PI*2);ctx.fill();ctx.strokeStyle="#b47d3d";ctx.beginPath();ctx.moveTo(p[0],p[1]);ctx.lineTo(p[0],p[1]+offset);ctx.stroke();label(text,p[0],p[1]+offset+(offset<0?-7:7),"#95652b")}
function axisGizmo(c){const x=width-45,y=55,axes=[[1,0,0],[0,1,0],[0,0,1]],colors=["#ad6e5b","#5c896c","#6b849b"];ctx.font='9px "Segoe UI",sans-serif';axes.forEach((a,i)=>{const dx=dot(a,c.right)*23,dy=-dot(a,c.up)*23;ctx.strokeStyle=colors[i];ctx.fillStyle=colors[i];ctx.beginPath();ctx.moveTo(x,y);ctx.lineTo(x+dx,y+dy);ctx.stroke();ctx.fillText("XYZ"[i],x+dx*1.3,y+dy*1.3)})}
function resize(){const bounds=stage.getBoundingClientRect(),dpr=Math.min(devicePixelRatio||1,2);width=bounds.width;height=bounds.height;for(const canvas of [scene,overlay]){canvas.width=Math.round(width*dpr);canvas.height=Math.round(height*dpr)}ctx.setTransform(dpr,0,0,dpr,0,0);draw()}
document.querySelectorAll("[data-state]").forEach(b=>b.addEventListener("click",()=>{current=b.dataset.state;update()}));
for(const id of ["ceiling","southCut","eastCut","guides"])$(id).addEventListener("change",draw);
$("resetView").addEventListener("click",()=>{az=-1.02;elev=.65;zoom=1;draw()});
scene.addEventListener("pointerdown",event=>{drag=[event.clientX,event.clientY];scene.setPointerCapture(event.pointerId)});
scene.addEventListener("pointermove",event=>{if(!drag)return;az-=(event.clientX-drag[0])*.008;elev=Math.max(.08,Math.min(1.5,elev+(event.clientY-drag[1])*.006));drag=[event.clientX,event.clientY];draw()});
scene.addEventListener("pointerup",()=>{drag=null});scene.addEventListener("pointercancel",()=>{drag=null});
scene.addEventListener("wheel",event=>{event.preventDefault();zoom=Math.max(.4,Math.min(2.4,zoom*Math.exp(-event.deltaY*.001)));draw()},{passive:false});
scene.addEventListener("keydown",event=>{if(!["ArrowLeft","ArrowRight","ArrowUp","ArrowDown","+","-"].includes(event.key))return;event.preventDefault();if(event.key==="ArrowLeft")az-=.1;if(event.key==="ArrowRight")az+=.1;if(event.key==="ArrowUp")elev=Math.min(1.5,elev+.08);if(event.key==="ArrowDown")elev=Math.max(.08,elev-.08);if(event.key==="+")zoom=Math.min(2.4,zoom*1.1);if(event.key==="-")zoom=Math.max(.4,zoom/1.1);draw()});
const checkNames={exact_area_perimeter:"精确面积 / 周长",geometry_text_cross_validation:"几何与标注交叉核验",all_serialized_models_verified:"GLB / OBJ / 3D DXF 重开核验",atomic_rollback:"失败操作原子回滚",dry_run_no_output:"预演不写出文件",explicit_no_overwrite:"拒绝隐式覆盖",missing_height_rejected:"拒绝缺少净高证据",misleading_text_rejected:"拒绝错误文字标注"};
$("checks").innerHTML=Object.entries(data.summary.checks).map(([key,value])=>`<span class="${value?"":"fail"}">${esc(checkNames[key]||key)}</span>`).join("");
$("manifest").innerHTML=data.manifest.map(a=>`<li><a href="${esc(a.href)}" title="${esc(a.path)}">${esc(a.label)}</a></li>`).join("");
$("overallBadge").textContent=data.summary.passed?"验收通过":"验收未通过";$("overallBadge").classList.toggle("fail",!data.summary.passed);
$("runName").textContent=data.run_name;
$("introText").textContent=`${data.states.after.spec.name} · ${data.provenance}`;
$("scopeText").textContent=`${data.provenance} ${data.summary.source_preserved?"验收记录确认源文件已保留。":"未获得源文件保留确认。"} ${data.summary.dwg_vs_dxf_semantics_match?"DWG 与 DXF 提取语义一致。":"未获得 DWG 与 DXF 语义一致确认。"}`;
$("renderText").textContent=data.renderer+` 图纸单位来自源证据：${data.states.after.spec.units}；GLB、OBJ 的坐标单位为 m，3D DXF 沿用图纸单位。`;
update();new ResizeObserver(resize).observe(stage);
</script>
</body>
</html>
'''


def build_viewer(run_dir: Path, output: Path, overwrite: bool = False) -> dict:
    if output.exists() and not overwrite:
        raise FileExistsError(f"Output exists; pass --overwrite explicitly: {output}")
    payload = build_payload(run_dir, output)
    # Escape HTML-significant characters even inside inert application/json.
    embedded = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    embedded = embedded.replace("&", "\\u0026").replace("<", "\\u003c").replace(
        ">", "\\u003e").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    rendered = HTML.replace("__EVIDENCE_JSON__", embedded)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8", newline="\n")
    return {"written": True, "output": str(output), "bytes": output.stat().st_size,
            "models": {key: {field: state["model"][field] for field in
                              ("component_count", "vertex_count", "triangle_count", "sha256")}
                       for key, state in payload["states"].items()},
            "network_dependencies": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True,
                        help="Completed acceptance directory containing summary.json")
    parser.add_argument("--output", type=Path, help="Default: <run-dir>/viewer.html")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    run_dir = args.run_dir.expanduser().resolve()
    output = (args.output or run_dir / "viewer.html").expanduser().resolve()
    print(json.dumps(build_viewer(run_dir, output, args.overwrite), ensure_ascii=False))


if __name__ == "__main__":
    main()
