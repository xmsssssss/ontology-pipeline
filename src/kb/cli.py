from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Annotated

import typer

from kb.ai_extraction import AISettings
from kb.constants import (
    DEFAULT_BASE_IRI,
    DEFAULT_ONTOLOGY_ID,
    DEFAULT_ONTOLOGY_VERSION,
)
from kb.graphdb import GraphDBClient, GraphDBSettings
from kb.pipeline import (
    build_owl_run,
    create_run,
    export_package,
    extract_run,
    finalize_run,
    parse_run,
    read_run,
    review_run,
    run_pipeline,
    segment_run,
    validate_run,
)
from kb.storage import Workspace

app = typer.Typer(
    no_args_is_help=True,
    help="规章文件到 OWL 本体的文件式流水线。",
)


def _print(value: object) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _ai_settings(
    *,
    model: str | None,
    base_url: str | None,
    batch_size: int | None,
    concurrency: int | None,
    extraction_strategy: str | None,
) -> AISettings:
    settings = AISettings.from_env()
    changes = {}
    if model:
        changes["model"] = model
    if base_url:
        changes["base_url"] = base_url.rstrip("/")
    if batch_size is not None:
        changes["batch_size"] = max(1, min(50, batch_size))
    if concurrency is not None:
        changes["concurrency"] = max(1, min(8, concurrency))
    if extraction_strategy in {"single_pass", "spires"}:
        changes["extraction_strategy"] = extraction_strategy
    return replace(settings, **changes) if changes else settings


@app.command()
def run(
    inputs: Annotated[
        list[Path],
        typer.Argument(help="输入文件或目录，可提供多个。"),
    ],
    workspace: Annotated[
        Path,
        typer.Option("--workspace", "-w", help="工作区目录。"),
    ] = Path("workspace"),
    run_id: Annotated[
        str | None,
        typer.Option("--run-id", help="指定运行 ID，便于自动化复现。"),
    ] = None,
    auto_approve: Annotated[
        bool,
        typer.Option("--auto-approve", help="自动批准全部抽取事实。"),
    ] = False,
    base_iri: Annotated[
        str,
        typer.Option("--base-iri", help="本体命名空间。"),
    ] = DEFAULT_BASE_IRI,
    ontology_id: Annotated[
        str,
        typer.Option("--ontology-id", help="本体 ID。"),
    ] = DEFAULT_ONTOLOGY_ID,
    version: Annotated[
        str,
        typer.Option("--version", help="本体版本。"),
    ] = DEFAULT_ONTOLOGY_VERSION,
    extraction_mode: Annotated[
        str,
        typer.Option("--extraction-mode", help="抽取模式：rule、ai 或 hybrid。"),
    ] = "rule",
    ai_model: Annotated[
        str | None,
        typer.Option("--ai-model", help="覆盖 AI_MODEL。"),
    ] = None,
    ai_base_url: Annotated[
        str | None,
        typer.Option("--ai-base-url", help="覆盖 AI_BASE_URL。"),
    ] = None,
    ai_batch_size: Annotated[
        int | None,
        typer.Option("--ai-batch-size", help="覆盖 AI_BATCH_SIZE。"),
    ] = None,
    ai_concurrency: Annotated[
        int | None,
        typer.Option("--ai-concurrency", help="覆盖 AI_CONCURRENCY。"),
    ] = None,
    ai_strategy: Annotated[
        str | None,
        typer.Option(
            "--ai-strategy",
            help="AI 抽取策略：spires 或 single_pass，默认读取 AI_EXTRACTION_STRATEGY。",
        ),
    ] = None,
) -> None:
    """执行扫描、解析、抽取、审核、OWL 构建、校验和打包。"""
    run_record, run_dir, manifest = run_pipeline(
        inputs,
        Workspace(workspace),
        run_id=run_id,
        auto_approve=auto_approve,
        base_iri=base_iri,
        ontology_id=ontology_id,
        version=version,
        extraction_mode=extraction_mode,
        ai_settings=_ai_settings(
            model=ai_model,
            base_url=ai_base_url,
            batch_size=ai_batch_size,
            concurrency=ai_concurrency,
            extraction_strategy=ai_strategy,
        ),
    )
    _print(
        {
            "run_id": run_record.run_id,
            "status": run_record.status,
            "artifact_dir": str(run_dir),
            "counts": manifest["counts"],
            "validation": manifest["validation"],
        }
    )
    if manifest["validation"]["status"] != "valid":
        raise typer.Exit(code=2)


@app.command()
def scan(
    inputs: Annotated[
        list[Path],
        typer.Argument(help="输入文件或目录。"),
    ],
    workspace: Annotated[
        Path,
        typer.Option("--workspace", "-w", help="工作区目录。"),
    ] = Path("workspace"),
    run_id: Annotated[
        str | None,
        typer.Option("--run-id", help="指定运行 ID。"),
    ] = None,
) -> None:
    """只执行文件扫描、哈希去重和原始文件归档。"""
    run_record, run_dir = create_run(
        inputs,
        Workspace(workspace),
        run_id=run_id,
    )
    _print(
        {
            "run_id": run_record.run_id,
            "status": run_record.status,
            "artifact_dir": str(run_dir),
            "counts": run_record.counts,
            "warnings": run_record.warnings,
        }
    )


@app.command()
def parse(
    run_dir: Annotated[Path, typer.Argument(help="scan 生成的运行目录。")],
) -> None:
    """解析扫描阶段登记的文件。"""
    _print(parse_run(run_dir))


@app.command()
def segment(
    run_dir: Annotated[Path, typer.Argument(help="parse 生成的运行目录。")],
) -> None:
    """按章节和条款编号切分非结构化文本。"""
    _print(segment_run(run_dir))


@app.command()
def extract(
    run_dir: Annotated[Path, typer.Argument(help="segment 生成的运行目录。")],
    extraction_mode: Annotated[
        str,
        typer.Option("--extraction-mode", help="抽取模式：rule、ai 或 hybrid。"),
    ] = "rule",
    ai_model: Annotated[
        str | None,
        typer.Option("--ai-model", help="覆盖 AI_MODEL。"),
    ] = None,
    ai_base_url: Annotated[
        str | None,
        typer.Option("--ai-base-url", help="覆盖 AI_BASE_URL。"),
    ] = None,
    ai_batch_size: Annotated[
        int | None,
        typer.Option("--ai-batch-size", help="覆盖 AI_BATCH_SIZE。"),
    ] = None,
    ai_concurrency: Annotated[
        int | None,
        typer.Option("--ai-concurrency", help="覆盖 AI_CONCURRENCY。"),
    ] = None,
    ai_strategy: Annotated[
        str | None,
        typer.Option(
            "--ai-strategy",
            help="AI 抽取策略：spires 或 single_pass，默认读取 AI_EXTRACTION_STRATEGY。",
        ),
    ] = None,
) -> None:
    """执行规则、AI 或混合实体与事实抽取。"""
    _print(
        extract_run(
            run_dir,
            extraction_mode=extraction_mode,
            ai_settings=_ai_settings(
                model=ai_model,
                base_url=ai_base_url,
                batch_size=ai_batch_size,
                concurrency=ai_concurrency,
                extraction_strategy=ai_strategy,
            ),
        )
    )


@app.command()
def review(
    run_dir: Annotated[Path, typer.Argument(help="extract 生成的运行目录。")],
    approve_all: Annotated[
        bool,
        typer.Option("--approve-all", help="批准全部事实。"),
    ] = False,
    approve: Annotated[
        list[str] | None,
        typer.Option("--approve", help="批准指定事实 ID，可重复提供。"),
    ] = None,
    reject: Annotated[
        list[str] | None,
        typer.Option("--reject", help="拒绝指定事实 ID，可重复提供。"),
    ] = None,
) -> None:
    """写入人工审核决定。"""
    approve_ids = approve or []
    reject_ids = reject or []
    if not approve_all and not approve_ids and not reject_ids:
        raise typer.BadParameter("至少提供 --approve-all、--approve 或 --reject。")
    _print(
        review_run(
            run_dir,
            approve_all=approve_all,
            approve=approve_ids,
            reject=reject_ids,
        )
    )


@app.command("build-owl")
def build_owl(
    run_dir: Annotated[Path, typer.Argument(help="包含已审核事实的运行目录。")],
    base_iri: Annotated[
        str,
        typer.Option("--base-iri", help="本体命名空间。"),
    ] = DEFAULT_BASE_IRI,
    ontology_id: Annotated[
        str,
        typer.Option("--ontology-id", help="本体 ID。"),
    ] = DEFAULT_ONTOLOGY_ID,
    version: Annotated[
        str,
        typer.Option("--version", help="本体版本。"),
    ] = DEFAULT_ONTOLOGY_VERSION,
) -> None:
    """从已批准事实生成 OWL、SHACL、映射和来源文件。"""
    _print(
        build_owl_run(
            run_dir,
            base_iri=base_iri,
            ontology_id=ontology_id,
            version=version,
        )
    )


@app.command()
def validate(
    run_dir: Annotated[Path, typer.Argument(help="build-owl 生成的运行目录。")],
) -> None:
    """执行 RDF、本体声明、来源和 SHACL 校验。"""
    report = validate_run(run_dir)
    _print(
        {
            "status": report["status"],
            "conforms": report["conforms"],
            "counts": report["counts"],
            "errors": report["errors"],
            "warnings": report["warnings"],
        }
    )
    if report["status"] != "valid":
        raise typer.Exit(code=2)


@app.command()
def finalize(
    run_dir: Annotated[Path, typer.Argument(help="待打包的运行目录。")],
) -> None:
    """生成 manifest 和 checksums。"""
    _print(finalize_run(run_dir))


@app.command()
def export(
    run_dir: Annotated[Path, typer.Argument(help="待导出的运行目录。")],
    output: Annotated[Path, typer.Argument(help="目标目录或 zip 路径。")],
    as_zip: Annotated[
        bool,
        typer.Option("--zip", help="导出为 zip 文件。"),
    ] = False,
) -> None:
    """导出完整本体制品包。"""
    target = export_package(run_dir, output, as_zip=as_zip)
    _print({"exported": str(target)})


@app.command()
def inspect(
    run_dir: Annotated[Path, typer.Argument(help="运行目录。")],
) -> None:
    """查看运行状态和计数。"""
    run_record = read_run(run_dir)
    _print(run_record.model_dump(mode="json"))


@app.command()
def serve(
    workspace: Annotated[
        Path,
        typer.Option("--workspace", "-w", help="工作区目录。"),
    ] = Path("workspace"),
    host: Annotated[
        str,
        typer.Option("--host", help="监听地址。"),
    ] = "127.0.0.1",
    port: Annotated[
        int,
        typer.Option("--port", help="监听端口。"),
    ] = 8000,
    reload: Annotated[
        bool,
        typer.Option("--reload", help="开发模式自动重载。"),
    ] = False,
) -> None:
    """启动本体管理 Web 服务。"""
    os.environ["KB_WORKSPACE"] = str(workspace.resolve())
    import uvicorn

    uvicorn.run(
        "kb.web:app",
        host=host,
        port=port,
        reload=reload,
        reload_dirs=[str(Path(__file__).resolve().parent)] if reload else None,
    )


@app.command("graphdb-status")
def graphdb_status(
    graphdb_url: Annotated[
        str,
        typer.Option("--graphdb-url", help="GraphDB 服务地址。"),
    ] = "http://localhost:7200",
    repository: Annotated[
        str,
        typer.Option("--repository", help="GraphDB 仓库 ID。"),
    ] = "regulation",
    username: Annotated[
        str | None,
        typer.Option("--username", help="GraphDB 用户名。"),
    ] = None,
    password: Annotated[
        str | None,
        typer.Option("--password", help="GraphDB 密码。"),
    ] = None,
) -> None:
    """检查 GraphDB 连接和目标仓库。"""
    client = GraphDBClient(
        GraphDBSettings(
            base_url=graphdb_url,
            repository=repository,
            username=username,
            password=password,
        )
    )
    try:
        _print(client.status())
    finally:
        client.close()


@app.command()
def publish(
    run_dir: Annotated[Path, typer.Argument(help="已完成校验的运行目录。")],
    graphdb_url: Annotated[
        str,
        typer.Option("--graphdb-url", help="GraphDB 服务地址。"),
    ] = "http://localhost:7200",
    repository: Annotated[
        str,
        typer.Option("--repository", help="GraphDB 仓库 ID。"),
    ] = "regulation",
    username: Annotated[
        str | None,
        typer.Option("--username", help="GraphDB 用户名。"),
    ] = None,
    password: Annotated[
        str | None,
        typer.Option("--password", help="GraphDB 密码。"),
    ] = None,
) -> None:
    """把已校验运行中的 OWL 和 SHACL 发布到 GraphDB 命名图。"""
    directory = run_dir.resolve()
    validation_path = directory / "validation_report.json"
    if not validation_path.exists():
        raise typer.BadParameter("运行目录缺少 validation_report.json，请先执行 validate。")
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    if validation.get("status") != "valid":
        raise typer.BadParameter("运行未通过校验，不能发布。")
    run = read_run(directory)
    ontology_meta = getattr(run, "ontology", None)
    if not isinstance(ontology_meta, dict):
        raise typer.BadParameter("运行目录缺少本体元数据。")
    ontology_id = str(ontology_meta.get("id") or DEFAULT_ONTOLOGY_ID)
    version = str(ontology_meta.get("version") or DEFAULT_ONTOLOGY_VERSION)
    base_iri = str(ontology_meta.get("base_iri") or DEFAULT_BASE_IRI).rstrip("/")
    graph_iri = f"{base_iri}/graph/{ontology_id}/{version}"
    shapes_graph_iri = f"{graph_iri}/shapes"
    ontology_content = (directory / "ontology.ttl").read_text(encoding="utf-8")
    shapes_content = (directory / "shapes.ttl").read_text(encoding="utf-8")
    client = GraphDBClient(
        GraphDBSettings(
            base_url=graphdb_url,
            repository=repository,
            username=username,
            password=password,
        )
    )
    try:
        result = client.publish_graph(
            graph_iri=graph_iri,
            content=ontology_content,
            shapes_graph_iri=shapes_graph_iri,
            shapes_content=shapes_content,
        )
        _print(
            {
                **result,
                "repository": repository,
                "run_id": run.run_id,
                "ontology_id": ontology_id,
                "version": version,
            }
        )
    finally:
        client.close()
