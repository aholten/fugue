import inspect
from typing import Any, Dict, Iterable, Iterator, List, Optional, no_type_check

import pandas as pd
import pyarrow as pa
from triad import Schema, assert_or_throw
from triad.utils.iter import EmptyAwareIterable, make_empty_aware

from ..collections.function_wrapper import (
    AnnotatedParam,
    FunctionWrapper,
    _KeywordParam,
    _PositionalParam,
    annotated_param,
)
from .array_dataframe import ArrayDataFrame
from .arrow_dataframe import ArrowDataFrame
from .dataframe import DataFrame, LocalDataFrame
from .dataframe_iterable_dataframe import LocalDataFrameIterableDataFrame
from .dataframes import DataFrames
from .iterable_dataframe import IterableDataFrame
from .pandas_dataframe import PandasDataFrame
from .utils import to_local_df


class DataFrameFunctionWrapper(FunctionWrapper):
    @property
    def need_output_schema(self) -> Optional[bool]:
        return (
            self._rt.need_schema()
            if isinstance(self._rt, _DataFrameParamBase)
            else False
        )

    def get_format_hint(self) -> Optional[str]:
        for v in self._params.values():
            if isinstance(v, _DataFrameParamBase):
                if v.format_hint() is not None:
                    return v.format_hint()
        if isinstance(self._rt, _DataFrameParamBase):
            return self._rt.format_hint()
        return None

    def run(  # noqa: C901
        self,
        args: List[Any],
        kwargs: Dict[str, Any],
        ignore_unknown: bool = False,
        output_schema: Any = None,
        output: bool = True,
        ctx: Any = None,
    ) -> Any:
        p: Dict[str, Any] = {}
        for i in range(len(args)):
            p[self._params.get_key_by_index(i)] = args[i]
        p.update(kwargs)
        has_kw = False
        rargs: Dict[str, Any] = {}
        for k, v in self._params.items():
            if isinstance(v, (_PositionalParam, _KeywordParam)):
                if isinstance(v, _KeywordParam):
                    has_kw = True
            elif k in p:
                if isinstance(v, _DataFrameParamBase):
                    assert_or_throw(
                        isinstance(p[k], DataFrame),
                        lambda: TypeError(f"{p[k]} is not a DataFrame"),
                    )
                    rargs[k] = v.to_input_data(p[k], ctx=ctx)
                else:
                    rargs[k] = p[k]  # TODO: should we do auto type conversion?
                del p[k]
            elif v.required:
                raise ValueError(f"{k} is required by not given")
        if has_kw:
            rargs.update(p)
        elif not ignore_unknown and len(p) > 0:
            raise ValueError(f"{p} are not acceptable parameters")
        rt = self._func(**rargs)
        if not output:
            if isinstance(self._rt, _DataFrameParamBase):
                self._rt.count(rt)
            return
        if isinstance(self._rt, _DataFrameParamBase):
            return self._rt.to_output_df(rt, output_schema, ctx=ctx)
        return rt


class _DataFrameParamBase(AnnotatedParam):
    def __init__(self, param: Optional[inspect.Parameter]):
        super().__init__(param)
        assert_or_throw(self.required, lambda: TypeError(f"{self} must be required"))

    def to_input_data(self, df: DataFrame, ctx: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    def to_output_df(
        self, df: Any, schema: Any, ctx: Any
    ) -> DataFrame:  # pragma: no cover
        raise NotImplementedError

    def count(self, df: Any) -> int:  # pragma: no cover
        raise NotImplementedError

    def need_schema(self) -> Optional[bool]:
        return False

    def format_hint(self) -> Optional[str]:
        return None


@annotated_param(DataFrame, "d", child_can_reuse_code=True)
class DataFrameParam(_DataFrameParamBase):
    def to_input_data(self, df: DataFrame, ctx: Any) -> Any:
        return df

    def to_output_df(self, output: Any, schema: Any, ctx: Any) -> DataFrame:
        assert_or_throw(
            schema is None or output.schema == schema,
            lambda: f"Output schema mismatch {output.schema} vs {schema}",
        )
        return output

    def count(self, df: Any) -> int:
        if df.is_bounded:
            return df.count()
        else:
            return sum(1 for _ in df.as_array_iterable())


@annotated_param(LocalDataFrame, "l", child_can_reuse_code=True)
class LocalDataFrameParam(DataFrameParam):
    def to_input_data(self, df: DataFrame, ctx: Any) -> LocalDataFrame:
        return to_local_df(df)

    def to_output_df(self, output: LocalDataFrame, schema: Any, ctx: Any) -> DataFrame:
        assert_or_throw(
            schema is None or output.schema == schema,
            lambda: f"Output schema mismatch {output.schema} vs {schema}",
        )
        return output

    def count(self, df: LocalDataFrame) -> int:
        if df.is_bounded:
            return df.count()
        else:
            return sum(1 for _ in df.as_array_iterable())


@annotated_param("[NoSchema]", "s", matcher=lambda x: False, child_can_reuse_code=True)
class _LocalNoSchemaDataFrameParam(LocalDataFrameParam):
    def need_schema(self) -> Optional[bool]:
        return True


@annotated_param(List[List[Any]])
class _ListListParam(_LocalNoSchemaDataFrameParam):
    @no_type_check
    def to_input_data(self, df: DataFrame, ctx: Any) -> List[List[Any]]:
        return df.as_array(type_safe=True)

    @no_type_check
    def to_output_df(self, output: List[List[Any]], schema: Any, ctx: Any) -> DataFrame:
        return ArrayDataFrame(output, schema)

    @no_type_check
    def count(self, df: List[List[Any]]) -> int:
        return len(df)


@annotated_param(
    Iterable[List[Any]],
    matcher=lambda x: x == Iterable[List[Any]] or x == Iterator[List[Any]],
)
class _IterableListParam(_LocalNoSchemaDataFrameParam):
    @no_type_check
    def to_input_data(self, df: DataFrame, ctx: Any) -> Iterable[List[Any]]:
        return df.as_array_iterable(type_safe=True)

    @no_type_check
    def to_output_df(
        self, output: Iterable[List[Any]], schema: Any, ctx: Any
    ) -> DataFrame:
        return IterableDataFrame(output, schema)

    @no_type_check
    def count(self, df: Iterable[List[Any]]) -> int:
        return sum(1 for _ in df)


@annotated_param(EmptyAwareIterable[List[Any]])
class _EmptyAwareIterableListParam(_LocalNoSchemaDataFrameParam):
    @no_type_check
    def to_input_data(self, df: DataFrame, ctx: Any) -> EmptyAwareIterable[List[Any]]:
        return make_empty_aware(df.as_array_iterable(type_safe=True))

    @no_type_check
    def to_output_df(
        self, output: EmptyAwareIterable[List[Any]], schema: Any, ctx: Any
    ) -> DataFrame:
        return IterableDataFrame(output, schema)

    @no_type_check
    def count(self, df: EmptyAwareIterable[List[Any]]) -> int:
        return sum(1 for _ in df)


@annotated_param(List[Dict[str, Any]])
class _ListDictParam(_LocalNoSchemaDataFrameParam):
    @no_type_check
    def to_input_data(self, df: DataFrame, ctx: Any) -> List[Dict[str, Any]]:
        return list(to_local_df(df).as_dict_iterable())

    @no_type_check
    def to_output_df(
        self, output: List[Dict[str, Any]], schema: Any, ctx: Any
    ) -> DataFrame:
        schema = schema if isinstance(schema, Schema) else Schema(schema)

        def get_all() -> Iterable[List[Any]]:
            for row in output:
                yield [row[x] for x in schema.names]

        return IterableDataFrame(get_all(), schema)

    @no_type_check
    def count(self, df: List[Dict[str, Any]]) -> int:
        return len(df)


@annotated_param(
    Iterable[Dict[str, Any]],
    matcher=lambda x: x == Iterable[Dict[str, Any]] or x == Iterator[Dict[str, Any]],
)
class _IterableDictParam(_LocalNoSchemaDataFrameParam):
    @no_type_check
    def to_input_data(self, df: DataFrame, ctx: Any) -> Iterable[Dict[str, Any]]:
        return df.as_dict_iterable()

    @no_type_check
    def to_output_df(
        self, output: Iterable[Dict[str, Any]], schema: Any, ctx: Any
    ) -> DataFrame:
        schema = schema if isinstance(schema, Schema) else Schema(schema)

        def get_all() -> Iterable[List[Any]]:
            for row in output:
                yield [row[x] for x in schema.names]

        return IterableDataFrame(get_all(), schema)

    @no_type_check
    def count(self, df: Iterable[Dict[str, Any]]) -> int:
        return sum(1 for _ in df)


@annotated_param(EmptyAwareIterable[Dict[str, Any]])
class _EmptyAwareIterableDictParam(_LocalNoSchemaDataFrameParam):
    @no_type_check
    def to_input_data(
        self, df: DataFrame, ctx: Any
    ) -> EmptyAwareIterable[Dict[str, Any]]:
        return make_empty_aware(df.as_dict_iterable())

    @no_type_check
    def to_output_df(
        self, output: EmptyAwareIterable[Dict[str, Any]], schema: Any, ctx: Any
    ) -> DataFrame:
        schema = schema if isinstance(schema, Schema) else Schema(schema)

        def get_all() -> Iterable[List[Any]]:
            for row in output:
                yield [row[x] for x in schema.names]

        return IterableDataFrame(get_all(), schema)

    @no_type_check
    def count(self, df: EmptyAwareIterable[Dict[str, Any]]) -> int:
        return sum(1 for _ in df)


@annotated_param(pd.DataFrame, "p")
class _PandasParam(LocalDataFrameParam):
    @no_type_check
    def to_input_data(self, df: DataFrame, ctx: Any) -> pd.DataFrame:
        return df.as_pandas()

    @no_type_check
    def to_output_df(self, output: pd.DataFrame, schema: Any, ctx: Any) -> DataFrame:
        return PandasDataFrame(output, schema)

    @no_type_check
    def count(self, df: pd.DataFrame) -> int:
        return df.shape[0]

    def format_hint(self) -> Optional[str]:
        return "pandas"


@annotated_param(
    Iterable[pd.DataFrame],
    matcher=lambda x: x == Iterable[pd.DataFrame] or x == Iterator[pd.DataFrame],
)
class _IterablePandasParam(LocalDataFrameParam):
    @no_type_check
    def to_input_data(self, df: DataFrame, ctx: Any) -> Iterable[pd.DataFrame]:
        if not isinstance(df, LocalDataFrameIterableDataFrame):
            yield df.as_pandas()
        else:
            for sub in df.native:
                yield sub.as_pandas()

    @no_type_check
    def to_output_df(
        self, output: Iterable[pd.DataFrame], schema: Any, ctx: Any
    ) -> DataFrame:
        def dfs():
            for df in output:
                yield PandasDataFrame(df, schema)

        return LocalDataFrameIterableDataFrame(dfs())

    @no_type_check
    def count(self, df: Iterable[pd.DataFrame]) -> int:
        return sum(_.shape[0] for _ in df)

    def format_hint(self) -> Optional[str]:
        return "pandas"


@annotated_param(pa.Table)
class _PyArrowTableParam(LocalDataFrameParam):
    def to_input_data(self, df: DataFrame, ctx: Any) -> Any:
        return df.as_arrow()

    def to_output_df(self, output: Any, schema: Any, ctx: Any) -> DataFrame:
        assert isinstance(output, pa.Table)
        return ArrowDataFrame(output, schema=schema)

    def count(self, df: Any) -> int:  # pragma: no cover
        return df.count()

    def format_hint(self) -> Optional[str]:
        return "pyarrow"


@annotated_param(
    Iterable[pa.Table],
    matcher=lambda x: x == Iterable[pa.Table] or x == Iterator[pa.Table],
)
class _IterableArrowParam(LocalDataFrameParam):
    @no_type_check
    def to_input_data(self, df: DataFrame, ctx: Any) -> Iterable[pa.Table]:
        if not isinstance(df, LocalDataFrameIterableDataFrame):
            yield df.as_arrow()
        else:
            for sub in df.native:
                yield sub.as_arrow()

    @no_type_check
    def to_output_df(
        self, output: Iterable[pa.Table], schema: Any, ctx: Any
    ) -> DataFrame:
        def dfs():
            _schema: Optional[Schema] = None if schema is None else Schema(schema)
            for df in output:
                adf = ArrowDataFrame(df)
                if _schema is not None and not (  # pylint: disable-all
                    adf.schema == schema
                ):
                    adf = adf[_schema.names].alter_columns(_schema)
                yield adf

        return LocalDataFrameIterableDataFrame(dfs())

    @no_type_check
    def count(self, df: Iterable[pa.Table]) -> int:
        return sum(_.shape[0] for _ in df)

    def format_hint(self) -> Optional[str]:
        return "pyarrow"


@annotated_param(DataFrames, "c")
class _DataFramesParam(AnnotatedParam):
    pass
