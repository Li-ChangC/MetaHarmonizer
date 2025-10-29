from src.models import ontology_mapper_st as oms
from src.models import ontology_mapper_lm as oml
from src.models import ontology_mapper_rag as omr
from src.models import ontology_mapper_bi_encoder as ombe
import pandas as pd
import numpy as np
from thefuzz import fuzz
from src.CustomLogger.custom_logger import CustomLogger

logger = CustomLogger()
ABBR_DICT_PATH = "data/corpus/oncotree_code_to_name.csv"


class OntoMapEngine:
    """
    A class to initialize and run the OntoMapEngine for ontology mapping.

    Attributes:
        method (str): The name of the method.
        query (list[str]): The list of queries.
        corpus (list[str]): The list of corpus.
        cura_map (dict): The dictionary containing the mapping of queries to curated values.
        topk (int): The number of top matches to return.
        yaml_path (str): The path to the YAML file.
        om_strategy (str): The strategy to use for OntoMap.
        other_params (dict): Other parameters to pass to the engine.
        _test_or_prod (str): Indicates whether the environment is test or production.
        _logger (CustomLogger): Logger instance.
    """

    def __init__(self,
                 method: str,
                 category: str,
                 query: list[str],
                 corpus: list[str],
                 cura_map: dict,
                 topk: int = 5,
                 s2_strategy: str = 'lm',
                 s3_strategy: str = None,
                 s3_threshold: float = 0.9,
                 **other_params: dict) -> None:
        """
        Initializes the OntoMapEngine class.

        Args:
            method (str): The name of the method.
            query (list[str]): The list of queries.
            corpus (list[str]): The list of corpus.
            cura_map (dict): The dictionary containing the mapping of queries to curated values.
            topk (int, optional): The number of top matches to return. Defaults to 5.
            s2_strategy (str, optional): The strategy to use for stage 2 OntoMap. Defaults to 'lm'. Options are 'st' or 'lm'.
            s3_strategy (str, optional): The strategy to use for stage 3 OntoMap. Defaults to None. Options are 'rag', 'rag_bie', or None.
            s3_threshold (float, optional): The threshold for stage 3 OntoMap. Defaults to 0.8.
            **other_params (dict): Other parameters to pass to the engine.
        """
        self.method = method
        self.query = query
        self.category = category
        self.corpus = list(
            dict.fromkeys(corpus))  # Remove duplicates while preserving order
        self.topk = topk
        self.s2_strategy = s2_strategy
        self.s3_strategy = s3_strategy
        self.s3_threshold = s3_threshold
        self.cura_map = cura_map
        self.other_params = other_params
        if 'test_or_prod' not in self.other_params.keys():
            raise ValueError(
                "test_or_prod value must be defined in other_params dictionary"
            )

        self._test_or_prod = self.other_params['test_or_prod']
        self._logger = logger.custlogger(loglevel='INFO')

        corpus_df = self.other_params.get("corpus_df", None)

        if self.s2_strategy not in ('lm', 'st'):
            raise ValueError("s2_strategy must be 'lm' or 'st'")
        if self.s3_strategy is not None:
            if self.s3_strategy not in ('rag', 'rag_bie'):
                raise ValueError(
                    "s3_strategy must be 'rag', 'rag_bie', or None")
            if corpus_df is None:
                raise ValueError(
                    "corpus_df must be provided for 'rag'/'rag_bie' for running stage 3"
                )
            corpus_df = self._normalize_df(corpus_df, need_code=True)
            self.other_params["corpus_df"] = corpus_df
            self.corpus = corpus_df["official_label"].astype(
                str).unique().tolist()

        self._logger.info("Initialized OntoMap Engine")
        self._logger.info(f"Stage 1: Exact matching")
        self._logger.info(f"Stage 2: {self.s2_strategy.upper()}")
        if self.s3_strategy is not None:
            self._logger.info(
                f"Stage 3: {self.s3_strategy.upper()} (threshold={self.s3_threshold})"
            )
        else:
            self._logger.info("Stage 3: Disabled")

    def _normalize_df(self, df: pd.DataFrame, need_code: bool) -> pd.DataFrame:
        """
        Normalizes the input DataFrame by ensuring the presence of necessary columns and cleaning data.
        - official_label: if missing, attempts to use 'label' column as fallback;
        - clean_code: if missing, attempts to extract from 'obo_id' (format NCIT:C123456 → C123456);
        - If still missing, raises an error;
        - Removes null and duplicate values to ensure clean data.
        """
        df = df.copy()

        # 1️⃣ official_label fallback
        if "official_label" not in df.columns:
            if "label" in df.columns:
                df["official_label"] = df["label"]
                self._logger.info(
                    "`official_label` not found — using `label` as fallback.")
            else:
                raise ValueError(
                    "DataFrame must contain 'official_label' or 'label'")

        # 2️⃣ clean_code fallback
        if need_code:
            if "clean_code" not in df.columns:
                if "obo_id" in df.columns:
                    # Extract code part from obo_id, e.g., "NCIT:C156482" -> "C156482"
                    df["clean_code"] = df["obo_id"].astype(str).str.extract(
                        r'(C\d+)', expand=False)
                    self._logger.info(
                        "`clean_code` not found — generated from `obo_id`.")
                else:
                    raise ValueError(
                        "DataFrame must contain 'clean_code' or 'obo_id' for RAG/RAG_BIE strategies"
                    )

        # 3️⃣ Basic cleaning
        keep = ["official_label"] + (["clean_code"] if need_code else [])
        df = df.dropna(subset=keep).drop_duplicates(subset=keep)
        df["official_label"] = df["official_label"].astype(str)
        if "clean_code" in df.columns:
            df["clean_code"] = df["clean_code"].astype(str)

        return df

    def _exact_matching(self):
        """
        Performs exact matching of queries to the corpus.

        Returns:
            list: The list of exact matches from the query.
        """
        corpus_normalized = {c.strip().lower() for c in self.corpus}
        return [
            q for q in self.query if q.strip().lower() in corpus_normalized
        ]

    def _map_shortname_to_fullname(self, non_exact_list: list[str]) -> dict:
        """
        Return a dict: original_value -> updated_value
        (short name (code) → full name if exists, otherwise keep original)
        """
        try:
            mapping_df = pd.read_csv(ABBR_DICT_PATH)
            short_to_name = dict(
                zip(mapping_df["code"].str.strip(),
                    mapping_df["name"].str.strip()))
        except FileNotFoundError:
            self._logger.warning(
                "Abbreviation mapping file not found. Skipping abbreviation replacement."
            )
            short_to_name = {}

        replaced = {}
        for q in non_exact_list:
            q_strip = q.strip()
            replaced[q] = short_to_name.get(q_strip, q_strip)
            if q_strip in short_to_name:
                self._logger.info(
                    f"Replaced: {q_strip} → {short_to_name[q_strip]}")
        return replaced

    def _om_model_from_strategy(self, strategy: str,
                                non_exact_query_list: list[str]):
        """
        Returns the OntoMap model based on the strategy.

        Args:
            strategy (str): The strategy to use ('lm', 'st', 'rag', 'rag_bie').
            non_exact_query_list (list[str]): The list of non-exact query strings.

        Returns:
            object: The OntoMap model instance.
        """
        query_df = self.other_params.get('query_df', None)
        corpus_df = self.other_params.get('corpus_df', None)

        if strategy == 'lm':
            return oml.OntoMapLM(method=self.method,
                                 category=self.category,
                                 om_strategy='lm',
                                 query=non_exact_query_list,
                                 corpus=self.corpus,
                                 topk=self.topk,
                                 from_tokenizer=True)

        elif strategy == 'st':
            return oms.OntoMapST(method=self.method,
                                 category=self.category,
                                 om_strategy='st',
                                 query=non_exact_query_list,
                                 corpus=self.corpus,
                                 topk=self.topk,
                                 from_tokenizer=False)
        elif strategy == 'rag':
            return omr.OntoMapRAG(method=self.method,
                                  category=self.category,
                                  om_strategy='rag',
                                  query=non_exact_query_list,
                                  corpus=self.corpus,
                                  topk=self.topk,
                                  corpus_df=corpus_df)
        elif strategy == 'rag_bie':
            return ombe.OntoMapBIE(method=self.method,
                                   category=self.category,
                                   om_strategy='rag_bie',
                                   query=non_exact_query_list,
                                   corpus=self.corpus,
                                   topk=self.topk,
                                   query_df=query_df,
                                   corpus_df=corpus_df)
        else:
            raise ValueError(
                f"strategy should be 'st', 'lm', 'rag', or 'rag_bie', got '{strategy}'"
            )

    def run(self):
        """
        Runs the OntoMap Engine with multi-stage cascade.

        Returns:
            pd.DataFrame: A DataFrame containing results from all stages.
        """
        self._logger.info("=" * 50)
        self._logger.info("Starting Ontology Mapping")
        self._logger.info("=" * 50)

        # ========== Stage 1: Exact Matching ==========
        self._logger.info("Stage 1: Exact Matching")
        exact_matches = self._exact_matching()
        self._logger.info(f"Exact matches: {len(exact_matches)}")

        stage1_matches = exact_matches

        # Create DataFrame for Stage 1 matches
        exact_df = pd.DataFrame({'original_value': stage1_matches})
        exact_df['curated_ontology'] = exact_df['original_value'].map(
            self.cura_map).fillna(exact_df['original_value'])
        exact_df['match_level'] = 1
        exact_df['stage'] = 1
        for i in range(1, self.topk + 1):
            exact_df[f'top{i}_match'] = exact_df['curated_ontology']
            exact_df[f'top{i}_score'] = 1.00

        # Remaining queries for Stage 2
        non_exact_matches_ls = list(np.setdiff1d(self.query, stage1_matches))
        self._logger.info(
            f"Remaining for Stage 2: {len(non_exact_matches_ls)}")

        if not non_exact_matches_ls:
            self._logger.info(
                "No queries for Stage 2. Returning Stage 1 results.")
            return exact_df

        # ========== Stage 2: LM/ST ==========
        self._logger.info(f"Stage 2: {self.s2_strategy.upper()} Matching")
        self._logger.info("Replacing shortNames using rule-based name mapping")
        mapping_dict = self._map_shortname_to_fullname(non_exact_matches_ls)
        updated_queries = [mapping_dict[q] for q in non_exact_matches_ls]

        replace_df = pd.DataFrame({
            "original_value": non_exact_matches_ls,
            "updated_value": updated_queries
        })

        updated_cura_map = {
            mapping_dict[k]: v
            for k, v in self.cura_map.items() if k in mapping_dict
        }

        # Run Stage 2 model
        s2_model = self._om_model_from_strategy(self.s2_strategy,
                                                updated_queries)
        s2_res = s2_model.get_match_results(cura_map=updated_cura_map,
                                            topk=self.topk,
                                            test_or_prod=self._test_or_prod)

        # Merge back to original_value
        s2_res.rename(columns={"original_value": "updated_value"},
                      inplace=True)
        s2_res = pd.merge(replace_df, s2_res, on="updated_value", how="left")
        s2_res["curated_ontology"] = s2_res["original_value"].map(
            self.cura_map).fillna("Not Found")
        s2_res['stage'] = 2

        self._logger.info(f"Stage 2 completed: {len(s2_res)} queries")

        # ========== Stage 3: RAG/RAG_BIE (Optional) ==========
        if self.s3_strategy is None:
            # No Stage 3, combine Stage 1 + Stage 2
            self._logger.info("Stage 3: Disabled")
            combined_results = pd.concat([exact_df, s2_res], ignore_index=True)

            self._logger.info("FINAL SUMMARY")
            self._logger.info(f"Stage 1 (Exact): {len(exact_df)} queries")
            self._logger.info(
                f"Stage 2 ({self.s2_strategy.upper()}): {len(s2_res)} queries")

            return combined_results

        else:
            # Check which queries need Stage 3 (top1_score < threshold)
            self._logger.info(f"Stage 3: {self.s3_strategy.upper()} Matching")

            top1_score_col = 'top1_score'
            if top1_score_col not in s2_res.columns:
                self._logger.warning(
                    f"{top1_score_col} not found in Stage 2 results. Skipping Stage 3."
                )
                combined_results = pd.concat([exact_df, s2_res],
                                             ignore_index=True)
                return combined_results

            self._logger.info(f"S2 result columns: {s2_res.columns.tolist()}")
            self._logger.info(
                f"S2 result top1_score dtype: {s2_res['top1_score'].dtype}")
            self._logger.info(
                f"S2 result top1_score unique values (first 10): {s2_res['top1_score'].unique()[:10]}"
            )
            # Identify low-confidence queries
            low_confidence_mask = pd.to_numeric(
                s2_res[top1_score_col],
                errors='coerce').fillna(0) < self.s3_threshold
            queries_for_s3 = s2_res.loc[low_confidence_mask,
                                        'original_value'].tolist()

            self._logger.info(
                f"Queries with top1_score < {self.s3_threshold}: {len(queries_for_s3)}"
            )

            if not queries_for_s3:
                self._logger.info("No queries require Stage 3.")
                combined_results = pd.concat([exact_df, s2_res],
                                             ignore_index=True)

                self._logger.info("FINAL SUMMARY")
                self._logger.info(f"Stage 1 (Exact): {len(exact_df)} queries")
                self._logger.info(
                    f"Stage 2 ({self.s2_strategy.upper()}): {len(s2_res)} queries"
                )

                return combined_results

            # Apply shortname replacement for Stage 3 queries
            mapping_dict_s3 = self._map_shortname_to_fullname(queries_for_s3)
            updated_queries_s3 = [mapping_dict_s3[q] for q in queries_for_s3]

            replace_df_s3 = pd.DataFrame({
                "original_value": queries_for_s3,
                "updated_value": updated_queries_s3
            })

            updated_cura_map_s3 = {
                mapping_dict_s3[k]: v
                for k, v in self.cura_map.items() if k in mapping_dict_s3
            }

            # Run Stage 3 model
            s3_model = self._om_model_from_strategy(self.s3_strategy,
                                                    updated_queries_s3)
            s3_res = s3_model.get_match_results(
                cura_map=updated_cura_map_s3,
                topk=self.topk,
                test_or_prod=self._test_or_prod)

            # Merge back to original_value
            s3_res.rename(columns={"original_value": "updated_value"},
                          inplace=True)
            s3_res = pd.merge(replace_df_s3,
                              s3_res,
                              on="updated_value",
                              how="left")
            s3_res["curated_ontology"] = s3_res["original_value"].map(
                self.cura_map).fillna("Not Found")
            s3_res['stage'] = 3

            self._logger.info(f"Stage 3 completed: {len(s3_res)} queries")

            # Remove Stage 2 results for queries that went to Stage 3
            s2_res_filtered = s2_res[~s2_res['original_value'].
                                     isin(queries_for_s3)].copy()

            # Combine all stages: Stage 1 + Stage 2 (filtered) + Stage 3
            combined_results = pd.concat([exact_df, s2_res_filtered, s3_res],
                                         ignore_index=True)

            # Final summary
            self._logger.info("=" * 50)
            self._logger.info("FINAL SUMMARY")
            self._logger.info("=" * 50)
            self._logger.info(f"Stage 1 (Exact): {len(exact_df)} queries")
            self._logger.info(
                f"Stage 2 ({self.s2_strategy.upper()}): {len(s2_res_filtered)} queries"
            )
            self._logger.info(
                f"Stage 3 ({self.s3_strategy.upper()}): {len(s3_res)} queries")

            return combined_results
