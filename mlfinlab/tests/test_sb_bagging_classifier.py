"""
Test various functions regarding chapter 4: Sampling (Bootstrapping, Concurrency).
"""

import os
import unittest

import numpy as np
import pandas as pd

from mlfinlab.filters.filters import cusum_filter
from mlfinlab.labeling.labeling import get_events, add_vertical_barrier, get_bins
from mlfinlab.sampling.bootstrapping import seq_bootstrap
from mlfinlab.ensemble.sb_bagging_classifier import SequentiallyBootstrappedBaggingClassifier, \
    SequentiallyBootstrappedBaggingRegressor
from sklearn.metrics import precision_score, recall_score, roc_auc_score, accuracy_score, mean_absolute_error, \
    mean_squared_error, f1_score
from sklearn.ensemble import BaggingClassifier, BaggingRegressor, RandomForestClassifier, RandomForestRegressor
from mlfinlab.util.utils import get_daily_vol


class TestSequentiallyBootstrappedBagging(unittest.TestCase):
    """
    Test SequentiallyBootstrapped Bagging classifiers
    """

    def setUp(self):
        """
        Set the file path for the sample dollar bars data and get triple barrier events, generate features
        """
        project_path = os.path.dirname(__file__)
        self.path = project_path + '/test_data/dollar_bar_sample.csv'
        self.data = pd.read_csv(self.path, index_col='date_time')
        self.data.index = pd.to_datetime(self.data.index)

        # Compute moving averages
        fast_window = 20
        slow_window = 50

        self.data['fast_mavg'] = self.data['close'].rolling(window=fast_window, min_periods=fast_window,
                                                            center=False).mean()
        self.data['slow_mavg'] = self.data['close'].rolling(window=slow_window, min_periods=slow_window,
                                                            center=False).mean()

        # Compute sides
        self.data['side'] = np.nan

        long_signals = self.data['fast_mavg'] >= self.data['slow_mavg']
        short_signals = self.data['fast_mavg'] < self.data['slow_mavg']
        self.data.loc[long_signals, 'side'] = 1
        self.data.loc[short_signals, 'side'] = -1

        # Remove Look ahead bias by lagging the signal
        self.data['side'] = self.data['side'].shift(1)

        daily_vol = get_daily_vol(close=self.data['close'], lookback=50)
        cusum_events = cusum_filter(self.data['close'], threshold=0.001)
        vertical_barriers = add_vertical_barrier(t_events=cusum_events, close=self.data['close'],
                                                 num_days=2)
        self.meta_labeled_events = get_events(close=self.data['close'],
                                              t_events=cusum_events,
                                              pt_sl=[4, 4],
                                              target=daily_vol,
                                              min_ret=0.005,
                                              num_threads=3,
                                              vertical_barrier_times=vertical_barriers,
                                              side_prediction=self.data['side'])

        self.meta_labeled_events.dropna(inplace=True)
        labels = get_bins(self.meta_labeled_events, self.data['close'])

        # Feature generation
        features = []
        X = self.data.copy()
        X['log_ret'] = X.close.apply(np.log).diff()
        for win in [2, 5, 10, 20, 25]:
            X['momentum_{}'.format(win)] = X.close / X.close.rolling(window=win).mean() - 1
            X['std_{}'.format(win)] = X.log_ret.rolling(window=win).std()
            X['pct_change_{}'.format(win)] = X.close.pct_change(win)
            X['diff_{}'.format(win)] = X.close.diff(win)

            for f in ['momentum', 'std', 'pct_change', 'diff']:
                features.append('{}_{}'.format(f, win))

        # Train/test generation
        X.dropna(inplace=True)
        X = X.loc[self.meta_labeled_events.index, :]  # Take only filtered events
        labels = labels.loc[X.index, :]  # Sync X and y
        self.meta_labeled_events = self.meta_labeled_events.loc[X.index, :]  # Sync X and meta_labeled_events

        self.X_train, self.y_train_clf, self.y_train_reg = X.iloc[:300][features], labels.iloc[:300].bin, labels.iloc[
                                                                                                          :300].ret
        self.X_test, self.y_test_clf, self.y_test_reg = X.iloc[300:][features], labels.iloc[300:].bin, labels.iloc[
                                                                                                       300:].ret

        # Init classifiers
        clf = RandomForestClassifier(n_estimators=1, criterion='entropy', bootstrap=False,
                                     class_weight='balanced_subsample')
        reg = RandomForestRegressor(n_estimators=1, bootstrap=False)
        self.sb_clf = SequentiallyBootstrappedBaggingClassifier(base_estimator=clf, max_features=1.0, n_estimators=100,
                                                                triple_barrier_events=self.meta_labeled_events,
                                                                price_bars=self.data, oob_score=True)
        self.sb_reg = SequentiallyBootstrappedBaggingRegressor(base_estimator=reg, max_features=1.0, n_estimators=100,
                                                               triple_barrier_events=self.meta_labeled_events,
                                                               price_bars=self.data, oob_score=True)

        self.sklearn_clf = BaggingClassifier(base_estimator=clf, max_features=1.0, n_estimators=50, oob_score=True)
        self.sklearn_reg = BaggingRegressor(base_estimator=reg, max_features=1.0, n_estimators=50, oob_score=True)

    def test_sb_classifier(self):
        self.sb_clf.fit(self.X_train, self.y_train_clf)
        self.sklearn_clf.fit(self.X_train, self.y_train_clf)

        oos_sb_predictions = self.sb_clf.predict(self.X_test)
        oos_sklearn_predictions = self.sklearn_clf.predict(self.X_test)

        sb_precision = precision_score(self.y_test_clf, oos_sb_predictions)
        sb_recall = recall_score(self.y_test_clf, oos_sb_predictions)
        sb_f1 = f1_score(self.y_test_clf, oos_sb_predictions)
        sb_roc_auc = roc_auc_score(self.y_test_clf, oos_sb_predictions)

        sklearn_precision = precision_score(self.y_test_clf, oos_sklearn_predictions)
        sklearn_recall = recall_score(self.y_test_clf, oos_sklearn_predictions)
        sklearn_f1 = f1_score(self.y_test_clf, oos_sklearn_predictions)
        sklearn_roc_auc = roc_auc_score(self.y_test_clf, oos_sklearn_predictions)

        # Test OOB scores (sequentially bootstrapped, algorithm specific, standard (random sampling)

        # Algorithm specific
        self.assertGreater(self.sb_clf.oob_score_, self.sklearn_clf.oob_score_)  # oob_score for SB should be greater
        self.assertAlmostEqual(self.sb_clf.oob_score_, 0.99, delta=0.01)

        # Sequentially Bootstrapped oob_score
        # Trim index map so that only train indices are present
        subsampled_ind_mat = self.sb_clf.ind_mat[:,
                             self.sb_clf.timestamp_int_index_mapping.loc[self.sb_clf.X_time_index]]
        sb_sample = seq_bootstrap(subsampled_ind_mat, sample_length=self.X_train.shape[0], compare=True)
        sb_clf_accuracy = accuracy_score(self.y_train_clf.iloc[sb_sample],
                                         self.sb_clf.predict(self.X_train.iloc[sb_sample]))
        sklearn_clf_accuracy = accuracy_score(self.y_train_clf.iloc[sb_sample],
                                              self.sklearn_clf.predict(self.X_train.iloc[sb_sample]))
        self.assertGreaterEqual(sb_clf_accuracy, sklearn_clf_accuracy)

        # Random sampling oob_score
        random_sample = np.random.choice(subsampled_ind_mat.shape[1], size=self.X_train.shape[0])
        sb_clf_accuracy = accuracy_score(self.y_train_clf.iloc[random_sample],
                                         self.sb_clf.predict(self.X_train.iloc[sb_sample]))
        sklearn_clf_accuracy = accuracy_score(self.y_train_clf.iloc[random_sample],
                                              self.sklearn_clf.predict(self.X_train.iloc[sb_sample]))

        self.assertTrue(sb_clf_accuracy >= sklearn_clf_accuracy)

        # Test that OOB metrics for SB are greater than sklearn's
        self.assertGreaterEqual(sb_precision, sklearn_precision)
        self.assertGreaterEqual(sb_recall, sklearn_recall)
        self.assertGreaterEqual(sb_f1, sklearn_f1)
        self.assertGreaterEqual(sb_roc_auc, sklearn_roc_auc)

    def test_sb_regressor(self):
        pass
