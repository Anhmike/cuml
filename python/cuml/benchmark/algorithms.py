#
# Copyright (c) 2019, NVIDIA CORPORATION.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import cuml

import sklearn
import sklearn.cluster
import sklearn.neighbors
import sklearn.ensemble
import sklearn.random_projection
from sklearn import metrics
import cuml.metrics
import cuml.decomposition
from cuml.utils.import_utils import has_umap
import numpy as np

from cuml.benchmark.bench_helper_funcs import (
    fit,
    fit_kneighbors,
    fit_transform,
    predict,
    _build_fil_classifier,
    _build_treelite_classifier,
    _treelite_fil_accuracy_score,
)
from cuml.utils.import_utils import has_treelite

if has_treelite():
    import treelite
    import treelite.runtime

if has_umap():
    import umap


class AlgorithmPair:
    """
    Wraps a cuML algorithm and (optionally) a cpu-based algorithm
    (typically scikit-learn, but does not need to be as long as it offers
    `fit` and `predict` or `transform` methods).
    Provides mechanisms to run each version with default arguments.
    If no CPU-based version of the algorithm is available, pass None for the
    cpu_class when instantiating
    """

    def __init__(
        self,
        cpu_class,
        cuml_class,
        shared_args,
        cuml_args={},
        cpu_args={},
        name=None,
        accepts_labels=True,
        cpu_data_prep_hook=None,
        cuml_data_prep_hook=None,
        accuracy_function=None,
        bench_func=fit,
        setup_cpu_func=None,
        setup_cuml_func=None,
    ):
        """
        Parameters
        ----------
        cpu_class : class
           Class for CPU version of algorithm. Set to None if not available.
        cuml_class : class
           Class for cuML algorithm
        shared_args : dict
           Arguments passed to both implementations's initializer
        cuml_args : dict
           Arguments *only* passed to cuml's initializer
        cpu_args dict
           Arguments *only* passed to sklearn's initializer
        accepts_labels : boolean
           If True, the fit methods expects both X and y
           inputs. Otherwise, it expects only an X input.
        data_prep_hook : function (data -> data)
           Optional function to run on input data before passing to fit
        accuracy_function : function (y_test, y_pred)
           Function that returns a scalar representing accuracy
        bench_func : custom function to perform fit/predict/transform
                     calls.
        """
        if name:
            self.name = name
        else:
            self.name = cuml_class.__name__
        self.accepts_labels = accepts_labels
        self.bench_func = bench_func
        self.setup_cpu_func = setup_cpu_func
        self.setup_cuml_func = setup_cuml_func
        self.cpu_class = cpu_class
        self.cuml_class = cuml_class
        self.shared_args = shared_args
        self.cuml_args = cuml_args
        self.cpu_args = cpu_args
        self.cpu_data_prep_hook = cpu_data_prep_hook
        self.cuml_data_prep_hook = cuml_data_prep_hook
        self.accuracy_function = accuracy_function

    def __str__(self):
        return "AlgoPair:%s" % (self.name)

    def run_cpu(self, data, **override_args):
        """Runs the cpu-based algorithm's fit method on specified data"""
        if self.cpu_class is None:
            raise ValueError("No CPU implementation for %s" % self.name)

        all_args = {**self.shared_args, **self.cpu_args}
        all_args = {**all_args, **override_args}

        if "cpu_setup_result" not in all_args:
            cpu_obj = self.cpu_class(**all_args)
        else:
            cpu_obj = all_args["cpu_setup_result"]
        if self.cpu_data_prep_hook:
            data = self.cpu_data_prep_hook(data)
        if self.accepts_labels:
            self.bench_func(cpu_obj, data[0], data[1])
        else:
            self.bench_func(cpu_obj, data[0])

        return cpu_obj

    def run_cuml(self, data, **override_args):
        """Runs the cuml-based algorithm's fit method on specified data"""
        all_args = {**self.shared_args, **self.cuml_args}
        all_args = {**all_args, **override_args}

        if "cuml_setup_result" not in all_args:
            cuml_obj = self.cuml_class(**all_args)
        else:
            cuml_obj = all_args["cuml_setup_result"]
        if self.cuml_data_prep_hook:
            data = self.cuml_data_prep_hook(data)
        if self.accepts_labels:
            self.bench_func(cuml_obj, data[0], data[1])
        else:
            self.bench_func(cuml_obj, data[0])

        return cuml_obj

    def setup_cpu(self, data, **override_args):
        if self.setup_cpu_func is not None:
            all_args = {**self.shared_args, **self.cpu_args}
            all_args = {**all_args, **override_args}
            return {
                "cpu_setup_result": self.setup_cpu_func(
                    self.cpu_class, data, all_args
                )
            }
        else:
            return {}

    def setup_cuml(self, data, **override_args):
        if self.setup_cuml_func is not None:
            all_args = {**self.shared_args, **self.cuml_args}
            all_args = {**all_args, **override_args}
            return {
                "cuml_setup_result": self.setup_cuml_func(
                    self.cuml_class, data, all_args
                )
            }
        else:
            return {}


def _labels_to_int_hook(data):
    """Helper function converting labels to int32"""
    return data[0], data[1].astype(np.int32)


def _treelite_format_hook(data):
    """Helper function converting data into treelite format"""
    from cuml.utils.import_utils import has_treelite

    if has_treelite():
        import treelite
        import treelite.runtime
    else:
        raise ImportError("No treelite package found")
    return treelite.runtime.Batch.from_npy2d(data[0]), data[1]


def all_algorithms():
    """Returns all defined AlgorithmPair objects"""
    algorithms = [
        AlgorithmPair(
            sklearn.cluster.KMeans,
            cuml.cluster.KMeans,
            shared_args=dict(init="random", n_clusters=8, max_iter=300),
            name="KMeans",
            accepts_labels=False,
            accuracy_function=metrics.homogeneity_score,
        ),
        AlgorithmPair(
            sklearn.decomposition.PCA,
            cuml.PCA,
            shared_args=dict(n_components=10),
            name="PCA",
            accepts_labels=False,
        ),
        AlgorithmPair(
            sklearn.decomposition.TruncatedSVD,
            cuml.decomposition.tsvd.TruncatedSVD,
            shared_args=dict(n_components=10),
            name="tSVD",
            accepts_labels=False,
        ),
        AlgorithmPair(
            sklearn.random_projection.GaussianRandomProjection,
            cuml.random_projection.GaussianRandomProjection,
            shared_args=dict(n_components="auto"),
            name="GaussianRandomProjection",
            bench_func=fit_transform,
            accepts_labels=False,
        ),
        AlgorithmPair(
            sklearn.neighbors.NearestNeighbors,
            cuml.neighbors.NearestNeighbors,
            shared_args=dict(n_neighbors=1024),
            cpu_args=dict(algorithm="brute", n_jobs=-1),
            cuml_args={},
            name="NearestNeighbors",
            accepts_labels=False,
            bench_func=fit_kneighbors,
        ),
        AlgorithmPair(
            sklearn.cluster.DBSCAN,
            cuml.DBSCAN,
            shared_args=dict(eps=3, min_samples=2),
            cpu_args=dict(algorithm="brute"),
            name="DBSCAN",
            accepts_labels=False,
        ),
        AlgorithmPair(
            sklearn.linear_model.LinearRegression,
            cuml.linear_model.LinearRegression,
            shared_args={},
            name="LinearRegression",
            accepts_labels=True,
            accuracy_function=metrics.r2_score,
        ),
        AlgorithmPair(
            sklearn.linear_model.ElasticNet,
            cuml.linear_model.ElasticNet,
            shared_args={"alpha": 0.1, "l1_ratio": 0.5},
            name="ElasticNet",
            accepts_labels=True,
            accuracy_function=metrics.r2_score,
        ),
        AlgorithmPair(
            sklearn.linear_model.Lasso,
            cuml.linear_model.Lasso,
            shared_args={},
            name="Lasso",
            accepts_labels=True,
            accuracy_function=metrics.r2_score,
        ),
        AlgorithmPair(
            sklearn.linear_model.Ridge,
            cuml.linear_model.Ridge,
            shared_args={},
            name="Ridge",
            accepts_labels=True,
            accuracy_function=metrics.r2_score,
        ),
        AlgorithmPair(
            sklearn.linear_model.LogisticRegression,
            cuml.linear_model.LogisticRegression,
            shared_args=dict(solver="lbfgs"),
            name="LogisticRegression",
            accepts_labels=True,
            accuracy_function=metrics.accuracy_score,
        ),
        AlgorithmPair(
            sklearn.ensemble.RandomForestClassifier,
            cuml.ensemble.RandomForestClassifier,
            shared_args={"max_features": 1.0, "n_estimators": 10},
            name="RandomForestClassifier",
            accepts_labels=True,
            cpu_data_prep_hook=_labels_to_int_hook,
            cuml_data_prep_hook=_labels_to_int_hook,
            accuracy_function=metrics.accuracy_score,
        ),
        AlgorithmPair(
            sklearn.ensemble.RandomForestRegressor,
            cuml.ensemble.RandomForestRegressor,
            shared_args={"max_features": 1.0, "n_estimators": 10},
            name="RandomForestRegressor",
            accepts_labels=True,
            accuracy_function=metrics.r2_score,
        ),
        AlgorithmPair(
            sklearn.manifold.TSNE,
            cuml.manifold.TSNE,
            shared_args=dict(),
            name="TSNE",
            accepts_labels=False,
        ),
        AlgorithmPair(
            None,
            cuml.linear_model.MBSGDClassifier,
            shared_args={},
            cuml_args=dict(eta0=0.005, epochs=100),
            name="MBSGDClassifier",
            accepts_labels=True,
            accuracy_function=cuml.metrics.accuracy_score,
        ),
        AlgorithmPair(
            treelite if has_treelite() else None,
            cuml.ForestInference,
            shared_args=dict(num_rounds=10, max_depth=10),
            cuml_args=dict(
                fil_algo="BATCH_TREE_REORG",
                output_class=True,
                threshold=0.5,
                storage_type="AUTO",
            ),
            name="FIL",
            accepts_labels=False,
            setup_cpu_func=_build_treelite_classifier,
            setup_cuml_func=_build_fil_classifier,
            cpu_data_prep_hook=_treelite_format_hook,
            accuracy_function=_treelite_fil_accuracy_score,
            bench_func=predict,
        ),
    ]

    if has_umap():
        algorithms.append(
            AlgorithmPair(
                umap.UMAP,
                cuml.manifold.UMAP,
                shared_args=dict(n_neighbors=5, n_epochs=500),
                name="UMAP",
                accepts_labels=False,
                accuracy_function=cuml.metrics.trustworthiness,
            )
        )

    return algorithms


def algorithm_by_name(name):
    """Returns the algorithm pair with the name 'name' (case-insensitive)"""
    algos = all_algorithms()
    return next((a for a in algos if a.name.lower() == name.lower()), None)
