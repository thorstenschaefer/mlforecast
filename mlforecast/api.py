# AUTOGENERATED! DO NOT EDIT! File to edit: nbs/api.ipynb (unless otherwise specified).

__all__ = ['validate_data_format', 'read_data', 'fcst_from_config', 'perform_backtest', 'parse_config', 'setup_client',
           'run_forecast']

# Cell
import importlib
import inspect
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Tuple, Union

try:
    import dask.dataframe as dd
    from dask.dataframe import DataFrame as dd_Frame
    from dask.distributed import Client
except ImportError:
    class dd: pass  # type: ignore
    dd_Frame = type(None)
    class Client: pass  # type: ignore
try:
    from s3path import S3Path
except ImportError:
    class S3Path: pass  # type: ignore
try:
    from .distributed.forecast import DistributedForecast
except ImportError:
    class DistributedForecast: pass  # type: ignore

import pandas as pd
import yaml
from fastcore.script import Param, call_parse
from pandas.api.types import is_datetime64_dtype

from .core import predictions_flow
from .data_model import (ClusterConfig, DataConfig, DataFormat,
                         DistributedModelConfig, DistributedModelName,
                         FeaturesConfig, FlowConfig, ModelConfig,
                         _available_tfms)
from .forecast import Forecast

# Internal Cell
Frame = Union[pd.DataFrame, dd_Frame]

_available_tfms_kwargs = {name: list(inspect.signature(tfm).parameters)[1:]
                          for name, tfm in _available_tfms.items()}

# Cell
def validate_data_format(data: Frame) -> Frame:
    """Checks whether data is in the correct format and tries to fix it if possible."""
    if not isinstance(data, (pd.DataFrame, dd_Frame)):
        raise ValueError('data must be either pandas or dask dataframe.')
    if not data.index.name == 'unique_id':
        if 'unique_id' in data:
            data = data.set_index('unique_id')
        else:
            raise ValueError('unique_id not found in data.')
    if 'ds' not in data:
        raise ValueError('ds column not found in data.')
    if not is_datetime64_dtype(data['ds']):
        if isinstance(data, pd.DataFrame):
            data['ds'] = pd.to_datetime(data['ds'])
        else:
            data['ds'] = dd.to_datetime(data['ds'])
    if 'y' not in data:
        raise ValueError('y column not found in data.')
    return data

# Internal Cell
def _is_s3_path(path: str) -> bool:
    return path.startswith('s3://')

# Internal Cell
def _path_as_str(path: Union[Path, S3Path]) -> str:
    if isinstance(path, S3Path):
        return path.as_uri()
    return str(path)

# Cell
def read_data(config: DataConfig, is_distributed: bool) -> Frame:
    """Read data from `config.prefix/config.input`.

    If we're in distributed mode dask is used for IO, else pandas."""
    prefix = config.prefix
    path = S3Path.from_uri(prefix) if _is_s3_path(prefix) else Path(prefix)
    input_path = path/config.input
    io_module = dd if is_distributed else pd
    reader = getattr(io_module, f'read_{config.format}')
    read_path = _path_as_str(input_path)
    if io_module is dd and config.format is DataFormat.csv:
        read_path += '/*'
    data = reader(read_path)
    if (
        io_module is dd
        and config.format is DataFormat.parquet
        and data.index.name == 'unique_id'
        and pd.api.types.is_categorical_dtype(data.index)
    ):
        data.index = data.index.cat.as_known().as_ordered()
    return validate_data_format(data)

# Internal Cell
def _instantiate_transforms(config: FeaturesConfig) -> Dict:
    """Turn the function names into the actual functions and make sure their positional arguments are in order."""
    if config.lag_transforms is None:
        return {}
    lag_tfms = defaultdict(list)
    for lag, tfms in config.lag_transforms.items():
        for tfm in tfms:
            if isinstance(tfm, dict):
                [(tfm_name, tfm_kwargs)] = tfm.items()
            else:
                tfm_name, tfm_kwargs = tfm, ()
            if tfm_name not in _available_tfms:
                raise NotImplementedError(tfm_name)
            tfm_func = _available_tfms[tfm_name]
            tfm_args: Tuple[Any, ...] = ()
            for kwarg in _available_tfms_kwargs[tfm_name]:
                if kwarg in tfm_kwargs:
                    tfm_args += (tfm_kwargs[kwarg], )
            lag_tfms[lag].append((tfm_func, *tfm_args))
    return lag_tfms

# Internal Cell
def _fcst_from_local(model_config: ModelConfig,
                     flow_config: Dict) -> Forecast:
    module_name, model_cls = model_config.name.rsplit('.', maxsplit=1)
    module = importlib.import_module(module_name)
    model = getattr(module, model_cls)(**(model_config.params or {}))
    return Forecast(model, flow_config)


def _fcst_from_distributed(model_config: DistributedModelConfig,
                           flow_config: Dict) -> DistributedForecast:
    if model_config.name is DistributedModelName.LightGBM:
        from .distributed.models.lgb import LGBMForecast
        model_cls = LGBMForecast
    else:
        from .distributed.models.xgb import XGBForecast
        model_cls = XGBForecast  # type: ignore
    model = model_cls(**(model_config.params or {}))
    return DistributedForecast(model, flow_config)

# Cell
def fcst_from_config(config: FlowConfig) -> Union[Forecast, DistributedForecast]:
    """Instantiate Forecast class from config."""
    flow_config = config.features.dict()
    flow_config['lag_transforms'] = _instantiate_transforms(config.features)

    if config.local is not None:
        return _fcst_from_local(config.local.model, flow_config)
    # because of the config validation, either local or distributed will be not None
    return _fcst_from_distributed(config.distributed.model, flow_config)  # type: ignore

# Cell
def perform_backtest(fcst: Union[Forecast, DistributedForecast],
                     data: Frame,
                     config: FlowConfig,
                     output_path: Union[Path, S3Path]):
    """Performs backtesting of `fcst` using `data` and the strategy defined in `config`.
    Writes the results to `output_path`."""
    if config.backtest is None:
        return
    data_is_dask = isinstance(data, dd_Frame)
    results = fcst.backtest(data,
                            config.backtest.n_windows,
                            config.backtest.window_size,
                            predictions_flow)
    for i, result in enumerate(results):
        result = result.fillna(0)
        split_path = _path_as_str(output_path/f'valid_{i}')
        if not data_is_dask:
            split_path += f'.{config.data.format}'
        writer = getattr(result, f'to_{config.data.format}')
        writer(split_path)
        result['sq_err'] = (result['y'] - result['y_pred'])**2
        mse = result.groupby("unique_id")["sq_err"].mean().mean()
        if data_is_dask:
            mse = mse.compute()
        print(f'Split {i+1} MSE: {mse:.4f}')

# Cell
def parse_config(config_file: str) -> FlowConfig:
    """Create a `FlowConfig` object using the contents of `config_file`"""
    with open(config_file, 'r') as f:
        config = FlowConfig(**yaml.safe_load(f))
    return config

def setup_client(config: ClusterConfig) -> Client:
    """Spins up a cluster with the specifications defined in `config` and returns a client connected to it."""
    module_name, cluster_cls = config.class_name.rsplit('.', maxsplit=1)
    module = importlib.import_module(module_name)
    cluster = getattr(module, cluster_cls)(**config.class_kwargs)
    client = Client(cluster)
    n_workers = config.class_kwargs.get('n_workers', 0)
    client.wait_for_workers(n_workers)
    return client

# Cell
@call_parse
def run_forecast(config_file: Param('Configuration file', str)):  # type: ignore # NOQA
    """Run the forecasting pipeline using the configuration defined in `config_file`."""
    config = parse_config(config_file)
    is_distributed = config.distributed is not None
    if config.distributed is not None:  # mypy
        client = setup_client(config.distributed.cluster)
    try:
        data = read_data(config.data, is_distributed)
        prefix = config.data.prefix
        path = S3Path.from_uri(prefix) if _is_s3_path(prefix) else Path(prefix)
        output_path = path/config.data.output
        output_path.mkdir(exist_ok=True)

        fcst = fcst_from_config(config)
        if config.backtest is not None:
            perform_backtest(fcst, data, config, output_path)
        if config.forecast is not None:
            fcst.fit(data)
            preds = fcst.predict(config.forecast.horizon)
            writer = getattr(preds, f'to_{config.data.format}')
            write_path = _path_as_str(output_path/'forecast')
            if isinstance(data, pd.DataFrame):
                write_path += f'.{config.data.format}'
            writer(write_path)
    except Exception as e:
        raise e
    finally:
        if is_distributed:
            client.cluster.close()
            client.close()